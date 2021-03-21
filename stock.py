# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
from sql import Cast, Literal
from sql.functions import Substring, Position

from trytond import backend
from trytond.i18n import gettext
from trytond.model import ModelSQL, ModelView, Workflow, fields, tree
from trytond.pool import Pool, PoolMeta
from trytond.pyson import Eval, Id
from trytond.report import Report
from trytond.transaction import Transaction

from .exceptions import PackageError


class Configuration(metaclass=PoolMeta):
    __name__ = 'stock.configuration'
    package_sequence = fields.MultiValue(fields.Many2One(
            'ir.sequence', "Package Sequence", required=True,
            domain=[
                ('company', 'in', [Eval('context', {}).get('company'), None]),
                ('sequence_type', '=',
                    Id('stock_package', 'sequence_type_package')),
                ]))

    @classmethod
    def multivalue_model(cls, field):
        pool = Pool()
        if field == 'package_sequence':
            return pool.get('stock.configuration.sequence')
        return super(Configuration, cls).multivalue_model(field)

    @classmethod
    def default_package_sequence(cls, **pattern):
        return cls.multivalue_model(
            'package_sequence').default_package_sequence()


class ConfigurationSequence(metaclass=PoolMeta):
    __name__ = 'stock.configuration.sequence'
    package_sequence = fields.Many2One(
        'ir.sequence', "Package Sequence", required=True,
        domain=[
            ('company', 'in', [Eval('company', -1), None]),
            ('sequence_type', '=',
                Id('stock_package', 'sequence_type_package')),
            ],
        depends=['company'])

    @classmethod
    def __register__(cls, module_name):
        exist = backend.TableHandler.table_exist(cls._table)
        if exist:
            table = cls.__table_handler__(module_name)
            exist &= table.column_exist('package_sequence')

        super(ConfigurationSequence, cls).__register__(module_name)

        if not exist:
            cls._migrate_property([], [], [])

    @classmethod
    def _migrate_property(cls, field_names, value_names, fields):
        field_names.append('package_sequence')
        value_names.append('package_sequence')
        super(ConfigurationSequence, cls)._migrate_property(
            field_names, value_names, fields)

    @classmethod
    def default_package_sequence(cls):
        pool = Pool()
        ModelData = pool.get('ir.model.data')
        try:
            return ModelData.get_id('stock_package', 'sequence_package')
        except KeyError:
            return None


class Package(tree(), ModelSQL, ModelView):
    'Stock Package'
    __name__ = 'stock.package'
    _rec_name = 'code'
    code = fields.Char('Code', select=True, readonly=True, required=True)
    company = fields.Many2One('company.company', "Company", required=True)
    type = fields.Many2One(
        'stock.package.type', "Type", required=True,
        states={
            'readonly': Eval('state') == 'closed',
            },
        depends=['state'])
    shipment = fields.Reference(
        "Shipment", selection='get_shipment', select=True,
        states={
            'readonly': Eval('state') == 'closed',
            },
        domain=[
            ('company', '=', Eval('company', -1)),
            ],
        depends=['state', 'company'])
    moves = fields.One2Many('stock.move', 'package', 'Moves',
        domain=[
            ('company', '=', Eval('company', -1)),
            ('shipment', '=', Eval('shipment')),
            ('to_location.type', 'in', ['customer', 'supplier']),
            ('state', '!=', 'cancelled'),
            ],
        add_remove=[
            ('package', '=', None),
            ],
        states={
            'readonly': Eval('state') == 'closed',
            },
        depends=['company', 'shipment', 'state'])
    parent = fields.Many2One(
        'stock.package', "Parent", select=True, ondelete='CASCADE',
        domain=[
            ('company', '=', Eval('company', -1)),
            ('shipment', '=', Eval('shipment')),
            ],
        states={
            'readonly': Eval('state') == 'closed',
            },
        depends=['company', 'shipment', 'state'])
    children = fields.One2Many(
        'stock.package', 'parent', 'Children',
        domain=[
            ('company', '=', Eval('company', -1)),
            ('shipment', '=', Eval('shipment')),
            ],
        states={
            'readonly': Eval('state') == 'closed',
            },
        depends=['company', 'shipment', 'state'])
    state = fields.Function(fields.Selection([
                ('open', "Open"),
                ('closed', "Closed"),
                ], "State"), 'on_change_with_state')

    @classmethod
    def __register__(cls, module):
        pool = Pool()
        table_h = cls.__table_handler__(module)
        table = cls.__table__()
        company_exist = table_h.column_exist('company')
        cursor = Transaction().connection.cursor()

        super().__register__(module)

        # Migration from 5.8: add company
        if not company_exist:
            shipment_id = Cast(Substring(
                    table.shipment,
                    Position(',', table.shipment) + Literal(1)),
                cls.id.sql_type().base)
            for name in cls._get_shipment():
                Shipment = pool.get(name)
                shipment = Shipment.__table__()
                value = (shipment
                    .select(shipment.company,
                        where=shipment.id == shipment_id))
                cursor.execute(*table.update(
                        [table.company], [value],
                        where=table.shipment.like(name + ',%')))

    @classmethod
    def default_company(cls):
        return Transaction().context.get('company')

    @staticmethod
    def _get_shipment():
        'Return list of Model names for shipment Reference'
        return [
            'stock.shipment.out',
            'stock.shipment.in.return',
            ]

    @classmethod
    def get_shipment(cls):
        pool = Pool()
        Model = pool.get('ir.model')
        get_name = Model.get_name
        models = cls._get_shipment()
        return [(None, '')] + [(m, get_name(m)) for m in models]

    @fields.depends('shipment')
    def on_change_with_state(self, name=None):
        if (self.shipment
                and self.shipment.state in {'packed', 'done', 'cancelled'}):
            return 'closed'
        return 'open'

    @classmethod
    def create(cls, vlist):
        pool = Pool()
        Config = pool.get('stock.configuration')

        vlist = [v.copy() for v in vlist]
        config = Config(1)
        for values in vlist:
            values['code'] = config.package_sequence.get()
        return super(Package, cls).create(vlist)

    @classmethod
    def copy(cls, packages, default=None):
        if default is None:
            default = {}
        else:
            default = default.copy()
        default.setdefault('moves')
        return super().copy(packages, default=default)


class Type(ModelSQL, ModelView):
    'Stock Package Type'
    __name__ = 'stock.package.type'
    name = fields.Char('Name', required=True)


class Move(metaclass=PoolMeta):
    __name__ = 'stock.move'
    package = fields.Many2One(
        'stock.package', "Package", select=True,
        domain=[
            ('company', '=', Eval('company', -1)),
            ],
        states={
            'readonly': Eval('state') == 'cancelled',
            },
        depends=['company', 'state'])

    @classmethod
    def copy(cls, moves, default=None):
        if default is None:
            default = {}
        else:
            default = default.copy()
        default.setdefault('package')
        return super().copy(moves, default=default)

    @classmethod
    @ModelView.button
    @Workflow.transition('cancelled')
    def cancel(cls, moves):
        cls.write([m for m in moves if m.package], {'package': None})
        super().cancel(moves)


class PackageMixin(object):
    __slots__ = ()
    packages = fields.One2Many('stock.package', 'shipment', 'Packages',
        domain=[
            ('company', '=', Eval('company', -1)),
            ],
        states={
            'readonly': Eval('state') != 'picked',
            },
        depends=['company'])
    root_packages = fields.Function(fields.One2Many('stock.package',
            'shipment', 'Packages',
            domain=[
                ('company', '=', Eval('company', -1)),
                ('parent', '=', None),
                ],
            states={
                'readonly': Eval('state') != 'picked',
                },
            depends=['company']),
        'get_root_packages', setter='set_root_packages')

    def get_root_packages(self, name):
        return [p.id for p in self.packages if not p.parent]

    @classmethod
    def set_root_packages(cls, shipments, name, value):
        if not value:
            return
        cls.write(shipments, {
                'packages': value,
                })

    @classmethod
    def check_packages(cls, shipments):
        for shipment in shipments:
            if not shipment.packages:
                continue
            length = sum(len(p.moves) for p in shipment.packages)
            if len(list(shipment.packages_moves)) != length:
                raise PackageError(
                    gettext('stock_package.msg_package_mismatch',
                        shipment=shipment.rec_name))

    @property
    def packages_moves(self):
        raise NotImplementedError


class ShipmentOut(PackageMixin, object, metaclass=PoolMeta):
    __name__ = 'stock.shipment.out'

    @classmethod
    @ModelView.button
    @Workflow.transition('packed')
    def pack(cls, shipments):
        super(ShipmentOut, cls).pack(shipments)
        cls.check_packages(shipments)

    @classmethod
    @ModelView.button
    @Workflow.transition('done')
    def done(cls, shipments):
        super(ShipmentOut, cls).done(shipments)
        cls.check_packages(shipments)

    @property
    def packages_moves(self):
        return (m for m in self.outgoing_moves if m.state != 'cancelled')


class ShipmentInReturn(PackageMixin, object, metaclass=PoolMeta):
    __name__ = 'stock.shipment.in.return'

    @classmethod
    @ModelView.button
    @Workflow.transition('done')
    def done(cls, shipments):
        super(ShipmentInReturn, cls).done(shipments)
        cls.check_packages(shipments)

    @property
    def packages_moves(self):
        return (m for m in self.moves if m.state != 'cancelled')


class PackageLabel(Report):
    'Package Label'
    __name__ = 'stock.package.label'
