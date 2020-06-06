# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
from trytond.model import fields
from trytond.pool import Pool, PoolMeta
from trytond.pyson import Eval


class Sale(metaclass=PoolMeta):
    __name__ = 'sale.sale'

    def is_done(self):
        done = super(Sale, self).is_done()
        if done:
            if any(l.supply_state in {'', 'requested'}
                    for l in self.lines if l.supply_on_sale):
                return False
        return done

    def create_shipment(self, shipment_type):
        if shipment_type == 'out':
            # purchase requests must be created before shipments to get
            # information about requests during the shipments creation
            # like the supplier
            self.create_purchase_requests()
            self.create_move_from_supply()
        shipments = super(Sale, self).create_shipment(shipment_type)
        return shipments

    def create_purchase_requests(self):
        pool = Pool()
        PurchaseRequest = pool.get('purchase.request')
        Line = pool.get('sale.line')
        requests = []
        lines = []
        for line in self.lines:
            request = line.get_purchase_request()
            if not request:
                continue
            requests.append(request)
            assert not line.purchase_request
            line.purchase_request = request
            lines.append(line)
        PurchaseRequest.save(requests)
        Line.save(lines)

    def create_move_from_supply(self):
        'Set to draft move linked to supply'
        pool = Pool()
        Move = pool.get('stock.move')
        ShipmentOut = pool.get('stock.shipment.out')

        moves = []
        for line in self.lines:
            if line.supply_state in {'supplied', 'cancelled'}:
                for move in line.moves:
                    if move.state == 'staging':
                        moves.append(move)
        Move.draft(moves)
        shipments = {m.shipment for m in moves
            if isinstance(m.shipment, ShipmentOut)}
        ShipmentOut.wait(shipments)


class Line(metaclass=PoolMeta):
    __name__ = 'sale.line'

    purchase_request = fields.Many2One('purchase.request', 'Purchase Request',
        ondelete='SET NULL', readonly=True)
    supply_state = fields.Function(fields.Selection([
                ('', ""),
                ('requested', "Requested"),
                ('supplied', "Supplied"),
                ('cancelled', "Cancelled"),
                ], "Supply State",
            states={
                'invisible': ~Eval('supply_state'),
                }), 'get_supply_state')

    @property
    def has_supply(self):
        return bool(self.purchase_request)

    def get_supply_state(self, name):
        if self.purchase_request is not None:
            purchase_line = self.purchase_request.purchase_line
            if purchase_line is not None:
                purchase = purchase_line.purchase
                if purchase.state == 'cancelled':
                    return 'cancelled'
                elif purchase.state in ('processing', 'done'):
                    return 'supplied'
            return 'requested'
        return ''

    @classmethod
    def copy(cls, lines, default=None):
        if default is None:
            default = {}
        else:
            default = default.copy()
        default.setdefault('purchase_request', None)
        return super().copy(lines, default=default)

    @property
    def supply_on_sale(self):
        "Returns True if the sale line has to be supply by purchase request"
        if (self.type != 'line'
                or not self.product
                or self.quantity <= 0
                or any(m.state not in ['staging', 'cancelled']
                    for m in self.moves)):
            return False
        return self.product.supply_on_sale

    def get_move(self, shipment_type):
        move = super().get_move(shipment_type)
        if (move
                and shipment_type == 'out'
                and (self.supply_on_sale or self.has_supply)):
            if self.supply_state in {'', 'requested'}:
                move.state = 'staging'
        return move

    def _get_purchase_request_product_supplier_pattern(self):
        return {
            'company': self.sale.company.id,
            }

    def get_purchase_request(self):
        'Return purchase request for the sale line'
        pool = Pool()
        Uom = pool.get('product.uom')
        Request = pool.get('purchase.request')

        if (not self.supply_on_sale
                or self.purchase_request
                or not self.product.purchasable):
            return

        # Ensure to create the request for the maximum paid
        if self.sale.shipment_method == 'invoice':
            invoice_skips = (set(self.sale.invoices_ignored)
                | set(self.sale.invoices_recreated))
            invoice_lines = [l for l in self.invoice_lines
                if l.invoice not in invoice_skips]
            if (not invoice_lines
                    or any((not l.invoice) or l.invoice.state != 'paid'
                        for l in invoice_lines)):
                return

        product = self.product
        supplier, purchase_date = Request.find_best_supplier(product,
            self.shipping_date,
            **self._get_purchase_request_product_supplier_pattern())
        uom = product.purchase_uom or product.default_uom
        quantity = self._get_move_quantity('out')
        quantity = Uom.compute_qty(self.unit, quantity, uom)
        return Request(
            product=product,
            party=supplier,
            quantity=quantity,
            uom=uom,
            computed_quantity=quantity,
            computed_uom=uom,
            purchase_date=purchase_date,
            supply_date=self.shipping_date,
            company=self.sale.company,
            warehouse=self.warehouse,
            origin=self.sale,
            )

    def assign_supplied(self, location_quantities):
        '''
        Assign supplied move

        location_quantities will be updated according to assigned
        quantities.
        '''
        pool = Pool()
        Uom = pool.get('product.uom')
        Move = pool.get('stock.move')

        if self.supply_state != 'supplied':
            return
        moves = set()
        for move in self.moves:
            for inv_move in move.shipment.inventory_moves:
                if inv_move.product.id == self.product.id:
                    moves.add(inv_move)
        for move in moves:
            if move.state != 'draft':
                continue
            location_qties_converted = {}
            for location_id, quantity in location_quantities.items():
                location_qties_converted[location_id] = (
                    Uom.compute_qty(move.product.default_uom,
                        quantity, move.uom, round=False))
            to_pick = move.pick_product(location_qties_converted)

            picked_qties = sum(qty for _, qty in to_pick)
            if picked_qties < move.quantity:
                first = False
                Move.write([move], {
                        'quantity': move.quantity - picked_qties,
                        })
            else:
                first = True
            for from_location, qty in to_pick:
                values = {
                    'from_location': from_location.id,
                    'quantity': qty,
                    }
                if first:
                    Move.write([move], values)
                    Move.assign([move])
                else:
                    Move.assign(Move.copy([move], default=values))

                qty_default_uom = Uom.compute_qty(move.uom, qty,
                    move.product.default_uom, round=False)

                location_quantities[from_location] -= qty_default_uom
