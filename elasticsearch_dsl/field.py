import base64
import ipaddress

import collections

from datetime import date, datetime

import six
from dateutil import parser, tz
from six import itervalues, string_types
from six.moves import map

from .utils import DslBase, ObjectBase, AttrDict, AttrList
from .exceptions import ValidationException

def construct_field(name_or_field, **params):
    # {"type": "text", "analyzer": "snowball"}
    if isinstance(name_or_field, collections.Mapping):
        if params:
            raise ValueError('construct_field() cannot accept parameters when passing in a dict.')
        params = name_or_field.copy()
        if 'type' not in params:
            # inner object can be implicitly defined
            if 'properties' in params:
                name = 'object'
            else:
                raise ValueError('construct_field() needs to have a "type" key.')
        else:
            name = params.pop('type')
        return Field.get_dsl_class(name)(**params)

    # Text()
    if isinstance(name_or_field, Field):
        if params:
            raise ValueError('construct_field() cannot accept parameters when passing in a construct_field object.')
        return name_or_field

    # "text", analyzer="snowball"
    return Field.get_dsl_class(name_or_field)(**params)

class Field(DslBase):
    _type_name = 'field'
    _type_shortcut = staticmethod(construct_field)
    # all fields can be multifields
    _param_defs = {'fields': {'type': 'field', 'hash': True}}
    name = None
    _coerce = False

    def __init__(self, *args, **kwargs):
        self._multi = kwargs.pop('multi', False)
        self._required = kwargs.pop('required', False)
        super(Field, self).__init__(*args, **kwargs)

    def __getitem__(self, subfield):
        return self._params.get('fields', {})[subfield]

    def _serialize(self, data):
        return data

    def _deserialize(self, data):
        return data

    def _empty(self):
        return None

    def empty(self):
        if self._multi:
            return AttrList([])
        return self._empty()

    def serialize(self, data):
        if isinstance(data, (list, AttrList)):
            return list(map(self._serialize, data))
        return self._serialize(data)

    def deserialize(self, data):
        if isinstance(data, (list, AttrList)):
            data[:] = map(self._deserialize, data)
            return data
        return self._deserialize(data)

    def clean(self, data):
        if data is not None:
            data = self.deserialize(data)
        if data in (None, [], {}) and self._required:
            raise ValidationException("Value required for this field.")
        return data

    def to_dict(self):
        d = super(Field, self).to_dict()
        name, value = d.popitem()
        value['type'] = name
        return value

class CustomField(Field):
    name = 'custom'
    _coerce = True

    def to_dict(self):
        if isinstance(self.builtin_type, Field):
            return self.builtin_type.to_dict()

        d = super(CustomField, self).to_dict()
        d['type'] = self.builtin_type
        return d

class InnerObjectWrapper(ObjectBase):
    def __init__(self, mapping, **kwargs):
        # mimic DocType behavior with _doc_type.mapping
        super(AttrDict, self).__setattr__('_doc_type', type('Meta', (), {'mapping': mapping}))
        super(InnerObjectWrapper, self).__init__(**kwargs)


class InnerObject(object):
    " Common functionality for nested and object fields. "
    _param_defs = {'properties': {'type': 'field', 'hash': True}}
    _coerce = True

    def __init__(self, *args, **kwargs):
        self._doc_class = kwargs.pop('doc_class', InnerObjectWrapper)
        super(InnerObject, self).__init__(*args, **kwargs)

    def field(self, name, *args, **kwargs):
        self.properties[name] = construct_field(*args, **kwargs)
        return self
    # XXX: backwards compatible, will be removed
    property = field

    def _empty(self):
        return self._doc_class(self.properties)

    def _wrap(self, data):
        return self._doc_class(self.properties, **data)

    def empty(self):
        if self._multi:
            return AttrList([], self._wrap)
        return self._empty()

    def __getitem__(self, name):
        return self.properties[name]

    def __contains__(self, name):
        return name in self.properties

    def _collect_fields(self):
        " Iterate over all Field objects within, including multi fields. "
        for f in itervalues(self.properties.to_dict()):
            yield f
            # multi fields
            if hasattr(f, 'fields'):
                for inner_f in itervalues(f.fields.to_dict()):
                    yield inner_f
            # nested and inner objects
            if hasattr(f, '_collect_fields'):
                for inner_f in f._collect_fields():
                    yield inner_f

    def update(self, other_object):
        if not hasattr(other_object, 'properties'):
            # not an inner/nested object, no merge possible
            return

        our, other = self.properties, other_object.properties
        for name in other:
            if name in our:
                if hasattr(our[name], 'update'):
                    our[name].update(other[name])
                continue
            our[name] = other[name]

    def _deserialize(self, data):
        if data is None:
            return None
        # don't wrap already wrapped data
        if isinstance(data, self._doc_class):
            return data

        if isinstance(data, (list, AttrList)):
            data[:] = list(map(self._deserialize, data))
            return data

        if isinstance(data, AttrDict):
            data = data._d_

        return self._wrap(data)

    def _serialize(self, data):
        if data is None:
            return None
        return data.to_dict()

    def clean(self, data):
        data = super(InnerObject, self).clean(data)
        if data is None:
            return None
        if isinstance(data, (list, AttrList)):
            for d in data:
                d.full_clean()
        else:
            data.full_clean()
        return data


class Object(InnerObject, Field):
    name = 'object'

class Nested(InnerObject, Field):
    name = 'nested'

    def __init__(self, *args, **kwargs):
        # change the default for Nested fields
        kwargs.setdefault('multi', True)
        super(Nested, self).__init__(*args, **kwargs)

class Date(Field):
    name = 'date'
    _coerce = True

    def __init__(self, *args, **kwargs):
        self._default_timezone = kwargs.pop('default_timezone', None)
        if isinstance(self._default_timezone, string_types):
            self._default_timezone = tz.gettz(self._default_timezone)
        super(Date, self).__init__(*args, **kwargs)

    def _deserialize(self, data):
        if isinstance(data, string_types):
            try:
                data = parser.parse(data)
            except Exception as e:
                raise ValidationException('Could not parse date from the value (%r)' % data, e)

        if not data:
            return None

        if isinstance(data, datetime):
            if self._default_timezone and data.tzinfo is None:
                data = data.replace(tzinfo=self._default_timezone)
            return data
        if isinstance(data, date):
            return data
        if isinstance(data, int):
            # Divide by a float to preserve milliseconds on the datetime.
            return datetime.utcfromtimestamp(data / 1000.0)

        raise ValidationException('Could not parse date from the value (%r)' % data)

class String(Field):
    _param_defs = {
        'fields': {'type': 'field', 'hash': True},
        'analyzer': {'type': 'analyzer'},
        'search_analyzer': {'type': 'analyzer'},
    }
    name = 'string'

class Text(Field):
    _param_defs = {
        'fields': {'type': 'field', 'hash': True},
        'analyzer': {'type': 'analyzer'},
        'search_analyzer': {'type': 'analyzer'},
        'search_quote_analyzer': {'type': 'analyzer'},
    }
    name = 'text'

class Keyword(Field):
    _param_defs = {
        'fields': {'type': 'field', 'hash': True},
        'search_analyzer': {'type': 'analyzer'},
        'normalizer': {'type': 'normalizer'}
    }
    name = 'keyword'

class Boolean(Field):
    name = 'boolean'
    _coerce = True

    def _deserialize(self, data):
        if data is None:
            return None
        if data == "false":
            return False
        return bool(data)

    def clean(self, data):
        if data is not None:
            data = self.deserialize(data)
        if data is None and self._required:
            raise ValidationException("Value required for this field.")
        return data

class Float(Field):
    name = 'float'
    _coerce = True

    def _deserialize(self, data):
        if data is None:
            return None
        return float(data)

class HalfFloat(Float):
    name = 'half_float'

class ScaledFloat(Float):
    name = 'scaled_float'

    def __init__(self, scaling_factor, *args, **kwargs):
        super(ScaledFloat, self).__init__(scaling_factor=scaling_factor, *args, **kwargs)

class Double(Float):
    name = 'double'

class Integer(Field):
    name = 'integer'
    _coerce = True

    def _deserialize(self, data):
        if data is None:
            return None
        return int(data)

class Byte(Integer):
    name = 'byte'

class Short(Integer):
    name = 'short'

class Long(Integer):
    name = 'long'

class Ip(Field):
    name = 'ip'
    _coerce = True

    def _deserialize(self, data):
        if data is None:
            return None

        # the ipaddress library for pypy, python2.5 and 2.6 only accepts unicode.
        return ipaddress.ip_interface(six.u(data))

    def _serialize(self, data):
        if data is None:
            return None
        return str(data)

class Binary(Field):
    name = 'binary'
    _coerce = True

    def _deserialize(self, data):
        if data is None:
            return None
        return base64.b64decode(data)

    def _serialize(self, data):
        if data is None:
            return None
        return base64.b64encode(data)

class GeoPoint(Field):
    name = 'geo_point'

class GeoShape(Field):
    name = 'geo_shape'

class Completion(Field):
    name = 'completion'

class Percolator(Field):
    name = 'percolator'

class IntegerRange(Field):
    name = 'integer_range'

class FloatRange(Field):
    name = 'float_range'

class LongRange(Field):
    name = 'long_range'

class DoubleRange(Field):
    name = 'double_ranged'

class DateRange(Field):
    name = 'date_range'

class Join(Field):
    name = 'join'

class TokenCount(Field):
    name = 'token_count'

class Murmur3:
    name = 'murmur3'
