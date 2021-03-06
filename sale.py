# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
from decimal import Decimal
import math

from endicia import CalculatingPostageAPI
from endicia.tools import objectify_response
from endicia.exceptions import RequestError
from trytond.model import ModelView, fields
from trytond.pool import PoolMeta, Pool
from trytond.transaction import Transaction
from trytond.pyson import Eval
from trytond.exceptions import UserError


__all__ = ['Configuration', 'Sale', 'SaleLine']
__metaclass__ = PoolMeta


ENDICIA_PACKAGE_TYPES = [
    ('Documents', 'Documents'),
    ('Gift', 'Gift'),
    ('Merchandise', 'Merchandise'),
    ('Other', 'Other'),
    ('Sample', 'Sample')
]


class Configuration:
    'Sale Configuration'
    __name__ = 'sale.configuration'

    endicia_mailclass = fields.Many2One(
        'endicia.mailclass', 'Default MailClass',
    )
    endicia_label_subtype = fields.Selection([
        ('None', 'None'),
        ('Integrated', 'Integrated')
    ], 'Label Subtype')
    endicia_integrated_form_type = fields.Selection([
        (None, ''),
        ('Form2976', 'Form2976(Same as CN22)'),
        ('Form2976A', 'Form2976(Same as CP72)'),
    ], 'Integrated Form Type')
    endicia_include_postage = fields.Boolean('Include Postage ?')
    endicia_package_type = fields.Selection(
        ENDICIA_PACKAGE_TYPES, 'Package Content Type'
    )

    @staticmethod
    def default_endicia_label_subtype():
        # This is the default value as specified in Endicia doc
        return 'None'

    @staticmethod
    def default_endicia_integrated_form_type():
        return None

    @staticmethod
    def default_endicia_package_type():
        # This is the default value as specified in Endicia doc
        return 'Other'


class Sale:
    "Sale"
    __name__ = 'sale.sale'

    endicia_mailclass = fields.Many2One(
        'endicia.mailclass', 'MailClass', states={
            'readonly': ~Eval('state').in_(['draft', 'quotation']),
        }, depends=['state']
    )
    is_endicia_shipping = fields.Function(
        fields.Boolean('Is Endicia Shipping?', readonly=True),
        'get_is_endicia_shipping'
    )

    @staticmethod
    def default_endicia_mailclass():
        Config = Pool().get('sale.configuration')
        config = Config(1)
        return config.endicia_mailclass and config.endicia_mailclass.id or None

    @classmethod
    def __setup__(cls):
        super(Sale, cls).__setup__()
        cls._error_messages.update({
            'mailclass_missing':
                'Select a mailclass to ship using Endicia [USPS].'
        })
        cls._buttons.update({
            'update_endicia_shipment_cost': {
                'invisible': Eval('state') != 'quotation'
            }
        })

    def on_change_carrier(self):
        res = super(Sale, self).on_change_carrier()

        res['is_endicia_shipping'] = self.carrier and \
            self.carrier.carrier_cost_method == 'endicia'

        return res

    def _get_carrier_context(self):
        "Pass sale in the context"
        context = super(Sale, self)._get_carrier_context()

        if not self.carrier.carrier_cost_method == 'endicia':
            return context

        context = context.copy()
        context['sale'] = self.id
        return context

    def on_change_lines(self):
        """Pass a flag in context which indicates the get_sale_price method
        of endicia carrier not to calculate cost on each line change
        """
        with Transaction().set_context({'ignore_carrier_computation': True}):
            return super(Sale, self).on_change_lines()

    def apply_endicia_shipping(self):
        "Add a shipping line to sale for endicia"
        Sale = Pool().get('sale.sale')
        Currency = Pool().get('currency.currency')

        if self.carrier and self.carrier.carrier_cost_method == 'endicia':
            if not self.endicia_mailclass:
                self.raise_user_error('mailclass_missing')
            with Transaction().set_context(self._get_carrier_context()):
                shipment_cost_usd = self.carrier.get_sale_price()
                if not shipment_cost_usd[0]:
                    return
            # Convert the shipping cost to sale currency from USD
            usd, = Currency.search([('code', '=', 'USD')])
            shipment_cost = Currency.compute(
                usd, shipment_cost_usd[0], self.currency
            )
            Sale.write([self], {
                'lines': [
                    ('create', [{
                        'type': 'line',
                        'product': self.carrier.carrier_product.id,
                        'description': self.endicia_mailclass.name,
                        'quantity': 1,  # XXX
                        'unit': self.carrier.carrier_product.sale_uom.id,
                        'unit_price': Decimal(shipment_cost),
                        'shipment_cost': Decimal(shipment_cost),
                        'amount': Decimal(shipment_cost),
                        'taxes': [],
                        'sequence': 9999,  # XXX
                    }]),
                    ('delete', [
                        line for line in self.lines if line.shipment_cost
                    ]),
                ]
            })

    @classmethod
    def quote(cls, sales):
        res = super(Sale, cls).quote(sales)
        cls.update_endicia_shipment_cost(sales)
        return res

    @classmethod
    @ModelView.button
    def update_endicia_shipment_cost(cls, sales):
        "Updates the shipping line with new value if any"
        for sale in sales:
            sale.apply_endicia_shipping()

    def create_shipment(self, shipment_type):
        Shipment = Pool().get('stock.shipment.out')

        with Transaction().set_context(ignore_carrier_computation=True):
            # disable `carrier cost computation`(default behaviour) as cost
            # should only be computed after updating mailclass else error may
            # occur, with improper mailclass.
            shipments = super(Sale, self).create_shipment(shipment_type)
        if shipment_type == 'out' and shipments and self.carrier and \
                self.carrier.carrier_cost_method == 'endicia':
            Shipment.write(shipments, {
                'endicia_mailclass': self.endicia_mailclass.id,
                'is_endicia_shipping': self.is_endicia_shipping,
            })
        return shipments

    def _get_ship_from_address(self):
        """
        Usually the warehouse from which you ship
        """
        return self.warehouse.address

    def get_endicia_shipping_cost(self, mailclass=None):
        """Returns the calculated shipping cost as sent by endicia

        :param mailclass: endicia mailclass for which cost to be fetched

        :returns: The shipping cost in USD
        """
        EndiciaConfiguration = Pool().get('endicia.configuration')

        endicia_credentials = EndiciaConfiguration(1).get_endicia_credentials()

        if not mailclass and not self.endicia_mailclass:
            self.raise_user_error('mailclass_missing')

        from_address = self._get_ship_from_address()

        calculate_postage_request = CalculatingPostageAPI(
            mailclass=mailclass or self.endicia_mailclass.value,
            weightoz=sum(map(
                lambda line: line.get_weight_for_endicia(), self.lines
            )),
            from_postal_code=from_address.zip[:5],
            to_postal_code=self.shipment_address.zip[:5],
            to_country_code=self.shipment_address.country.code,
            accountid=endicia_credentials.account_id,
            requesterid=endicia_credentials.requester_id,
            passphrase=endicia_credentials.passphrase,
            test=endicia_credentials.is_test,
        )

        try:
            response = calculate_postage_request.send_request()
        except RequestError, e:
            self.raise_user_error(unicode(e))

        return Decimal(
            objectify_response(response).PostagePrice.get('TotalAmount')
        )

    def _get_endicia_mail_classes(self):
        """
        Returns list of endicia mailclass instances eligible for this sale

        Downstream module can decide the eligibility of mail classes for sale
        """
        Mailclass = Pool().get('endicia.mailclass')

        return Mailclass.search([])

    def _make_endicia_rate_line(self, carrier, mailclass, shipment_rate):
        """
        Build a rate tuple from shipment_rate and mailclass
        """
        Currency = Pool().get('currency.currency')

        usd, = Currency.search([('code', '=', 'USD')])
        write_vals = {
            'carrier': carrier.id,
            'endicia_mailclass': mailclass.id,
        }
        return (
            carrier._get_endicia_mailclass_name(mailclass),
            shipment_rate,
            usd,
            {},
            write_vals
        )

    def get_endicia_shipping_rates(self, silent=True):
        """
        Call the rates service and get possible quotes for shipment for eligible
        mail classes
        """
        Carrier = Pool().get('carrier')

        carrier, = Carrier.search(['carrier_cost_method', '=', 'endicia'])

        rate_lines = []
        for mailclass in self._get_endicia_mail_classes():
            try:
                cost = self.get_endicia_shipping_cost(mailclass=mailclass.value)
            except UserError:
                if not silent:
                    raise
                continue
            rate_lines.append(
                self._make_endicia_rate_line(carrier, mailclass, cost)
            )
        return filter(None, rate_lines)

    def get_is_endicia_shipping(self, name):
        """
        Check if shipping is from USPS
        """
        return self.carrier and self.carrier.carrier_cost_method == 'endicia'


class SaleLine:
    'Sale Line'
    __name__ = 'sale.line'

    @classmethod
    def __setup__(cls):
        super(SaleLine, cls).__setup__()
        cls._error_messages.update({
            'weight_required': 'Weight is missing on the product %s',
        })

    def get_weight_for_endicia(self):
        """
        Returns weight as required for endicia.
        """
        ProductUom = Pool().get('product.uom')

        if not self.product or self.product.type == 'service' \
                or self.quantity <= 0:
            return Decimal(0)

        if not self.product.weight:
            self.raise_user_error(
                'weight_required',
                error_args=(self.product.name,)
            )

        # Find the quantity in the default uom of the product as the weight
        # is for per unit in that uom
        if self.unit != self.product.default_uom:
            quantity = ProductUom.compute_qty(
                self.unit,
                self.quantity,
                self.product.default_uom
            )
        else:
            quantity = self.quantity

        weight = float(self.product.weight) * quantity

        # Endicia by default uses oz for weight purposes
        if self.product.weight_uom.symbol != 'oz':
            ounce, = ProductUom.search([('symbol', '=', 'oz')])
            weight = ProductUom.compute_qty(
                self.product.weight_uom,
                weight,
                ounce
            )
        return Decimal(math.ceil(weight))
