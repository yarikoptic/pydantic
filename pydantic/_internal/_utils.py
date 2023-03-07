"""
Bucket of reusable internal utilities.

This should be reduced as much as possible with functions only used in one place, moved to that place.
"""
from __future__ import annotations as _annotations

import keyword
import typing
import weakref
from collections import OrderedDict, defaultdict, deque
from copy import deepcopy
from itertools import zip_longest
from types import BuiltinFunctionType, CodeType, FunctionType, GeneratorType, LambdaType, ModuleType
from typing import Any, TypeVar

from pydantic_core import CoreSchema
from pydantic_core.core_schema import TypedDictField

from . import _repr, _typing_extra

if typing.TYPE_CHECKING:
    from ..dataclasses import Dataclass
    from ..main import BaseModel

    MappingIntStrAny = typing.Mapping[int | str, Any]
    AbstractSetIntStr = typing.AbstractSet[int | str]

__all__ = (
    'sequence_like',
    'lenient_isinstance',
    'lenient_issubclass',
    'is_valid_identifier',
    'deep_update',
    'update_not_none',
    'almost_equal_floats',
    'get_model',
    'to_camel',
    'smart_deepcopy',
    'ValueItems',
    'ClassAttribute',
    'ROOT_KEY',
    'LimitedDict',
    'dict_not_none',
    'AbstractSetIntStr',
    'MappingIntStrAny',
    'all_identical',
    'is_typed_dict_field',
    'is_core_schema',
    'get_type_ref',
)

ROOT_KEY = '__root__'
# these are types that are returned unchanged by deepcopy
IMMUTABLE_NON_COLLECTIONS_TYPES: set[type[Any]] = {
    int,
    float,
    complex,
    str,
    bool,
    bytes,
    type,
    _typing_extra.NoneType,
    FunctionType,
    BuiltinFunctionType,
    LambdaType,
    weakref.ref,
    CodeType,
    # note: including ModuleType will differ from behaviour of deepcopy by not producing error.
    # It might be not a good idea in general, but considering that this function used only internally
    # against default values of fields, this will allow to actually have a field with module as default value
    ModuleType,
    NotImplemented.__class__,
    Ellipsis.__class__,
}

# these are types that if empty, might be copied with simple copy() instead of deepcopy()
BUILTIN_COLLECTIONS: set[type[Any]] = {
    list,
    set,
    tuple,
    frozenset,
    dict,
    OrderedDict,
    defaultdict,
    deque,
}


def sequence_like(v: Any) -> bool:
    return isinstance(v, (list, tuple, set, frozenset, GeneratorType, deque))


def lenient_isinstance(o: Any, class_or_tuple: type[Any] | tuple[type[Any], ...] | None) -> bool:
    try:
        return isinstance(o, class_or_tuple)  # type: ignore[arg-type]
    except TypeError:
        return False


def lenient_issubclass(cls: Any, class_or_tuple: Any) -> bool:
    try:
        return isinstance(cls, type) and issubclass(cls, class_or_tuple)
    except TypeError:
        if isinstance(cls, _typing_extra.WithArgsTypes):
            return False
        raise  # pragma: no cover


def is_valid_identifier(identifier: str) -> bool:
    """
    Checks that a string is a valid identifier and not a Python keyword.
    :param identifier: The identifier to test.
    :return: True if the identifier is valid.
    """
    return identifier.isidentifier() and not keyword.iskeyword(identifier)


KeyType = TypeVar('KeyType')


def deep_update(mapping: dict[KeyType, Any], *updating_mappings: dict[KeyType, Any]) -> dict[KeyType, Any]:
    updated_mapping = mapping.copy()
    for updating_mapping in updating_mappings:
        for k, v in updating_mapping.items():
            if k in updated_mapping and isinstance(updated_mapping[k], dict) and isinstance(v, dict):
                updated_mapping[k] = deep_update(updated_mapping[k], v)
            else:
                updated_mapping[k] = v
    return updated_mapping


def dict_not_none(__pos: dict[str, Any] = None, **kwargs: Any) -> dict[str, Any]:
    return {k: v for k, v in (__pos or kwargs).items() if v is not None}


def update_not_none(mapping: dict[Any, Any], **update: Any) -> None:
    mapping.update({k: v for k, v in update.items() if v is not None})


def almost_equal_floats(value_1: float, value_2: float, *, delta: float = 1e-8) -> bool:
    """
    Return True if two floats are almost equal
    """
    return abs(value_1 - value_2) <= delta


def get_model(obj: type[BaseModel] | type[Dataclass]) -> type[BaseModel]:
    from ..main import BaseModel

    try:
        model_cls = obj.__pydantic_model__  # type: ignore
    except AttributeError:
        model_cls = obj

    if not issubclass(model_cls, BaseModel):
        raise TypeError('Unsupported type, must be either BaseModel or dataclass')
    return model_cls


def to_camel(string: str) -> str:
    return ''.join(word.capitalize() for word in string.split('_'))


def to_lower_camel(string: str) -> str:
    if len(string) >= 1:
        pascal_string = to_camel(string)
        return pascal_string[0].lower() + pascal_string[1:]
    return string.lower()


T = TypeVar('T')


def unique_list(
    input_list: list[T] | tuple[T, ...],
    *,
    name_factory: typing.Callable[[T], str] = str,
) -> list[T]:
    """
    Make a list unique while maintaining order.
    We update the list if another one with the same name is set
    (e.g. root validator overridden in subclass)
    """
    result: list[T] = []
    result_names: list[str] = []
    for v in input_list:
        v_name = name_factory(v)
        if v_name not in result_names:
            result_names.append(v_name)
            result.append(v)
        else:
            result[result_names.index(v_name)] = v

    return result


class ValueItems(_repr.Representation):
    """
    Class for more convenient calculation of excluded or included fields on values.
    """

    __slots__ = ('_items', '_type')

    def __init__(self, value: Any, items: AbstractSetIntStr | MappingIntStrAny) -> None:
        items = self._coerce_items(items)

        if isinstance(value, (list, tuple)):
            items = self._normalize_indexes(items, len(value))

        self._items: MappingIntStrAny = items

    def is_excluded(self, item: Any) -> bool:
        """
        Check if item is fully excluded.

        :param item: key or index of a value
        """
        return self.is_true(self._items.get(item))

    def is_included(self, item: Any) -> bool:
        """
        Check if value is contained in self._items

        :param item: key or index of value
        """
        return item in self._items

    def for_element(self, e: int | str) -> AbstractSetIntStr | MappingIntStrAny | None:
        """
        :param e: key or index of element on value
        :return: raw values for element if self._items is dict and contain needed element
        """

        item = self._items.get(e)
        return item if not self.is_true(item) else None

    def _normalize_indexes(self, items: MappingIntStrAny, v_length: int) -> dict[int | str, Any]:
        """
        :param items: dict or set of indexes which will be normalized
        :param v_length: length of sequence indexes of which will be

        >>> self._normalize_indexes({0: True, -2: True, -1: True}, 4)
        {0: True, 2: True, 3: True}
        >>> self._normalize_indexes({'__all__': True}, 4)
        {0: True, 1: True, 2: True, 3: True}
        """

        normalized_items: dict[int | str, Any] = {}
        all_items = None
        for i, v in items.items():
            if not (isinstance(v, typing.Mapping) or isinstance(v, typing.AbstractSet) or self.is_true(v)):
                raise TypeError(f'Unexpected type of exclude value for index "{i}" {v.__class__}')
            if i == '__all__':
                all_items = self._coerce_value(v)
                continue
            if not isinstance(i, int):
                raise TypeError(
                    'Excluding fields from a sequence of sub-models or dicts must be performed index-wise: '
                    'expected integer keys or keyword "__all__"'
                )
            normalized_i = v_length + i if i < 0 else i
            normalized_items[normalized_i] = self.merge(v, normalized_items.get(normalized_i))

        if not all_items:
            return normalized_items
        if self.is_true(all_items):
            for i in range(v_length):
                normalized_items.setdefault(i, ...)
            return normalized_items
        for i in range(v_length):
            normalized_item = normalized_items.setdefault(i, {})
            if not self.is_true(normalized_item):
                normalized_items[i] = self.merge(all_items, normalized_item)
        return normalized_items

    @classmethod
    def merge(cls, base: Any, override: Any, intersect: bool = False) -> Any:
        """
        Merge a ``base`` item with an ``override`` item.

        Both ``base`` and ``override`` are converted to dictionaries if possible.
        Sets are converted to dictionaries with the sets entries as keys and
        Ellipsis as values.

        Each key-value pair existing in ``base`` is merged with ``override``,
        while the rest of the key-value pairs are updated recursively with this function.

        Merging takes place based on the "union" of keys if ``intersect`` is
        set to ``False`` (default) and on the intersection of keys if
        ``intersect`` is set to ``True``.
        """
        override = cls._coerce_value(override)
        base = cls._coerce_value(base)
        if override is None:
            return base
        if cls.is_true(base) or base is None:
            return override
        if cls.is_true(override):
            return base if intersect else override

        # intersection or union of keys while preserving ordering:
        if intersect:
            merge_keys = [k for k in base if k in override] + [k for k in override if k in base]
        else:
            merge_keys = list(base) + [k for k in override if k not in base]

        merged: dict[int | str, Any] = {}
        for k in merge_keys:
            merged_item = cls.merge(base.get(k), override.get(k), intersect=intersect)
            if merged_item is not None:
                merged[k] = merged_item

        return merged

    @staticmethod
    def _coerce_items(items: AbstractSetIntStr | MappingIntStrAny) -> MappingIntStrAny:
        if isinstance(items, typing.Mapping):
            pass
        elif isinstance(items, typing.AbstractSet):
            items = dict.fromkeys(items, ...)
        else:
            class_name = getattr(items, '__class__', '???')
            raise TypeError(f'Unexpected type of exclude value {class_name}')
        return items

    @classmethod
    def _coerce_value(cls, value: Any) -> Any:
        if value is None or cls.is_true(value):
            return value
        return cls._coerce_items(value)

    @staticmethod
    def is_true(v: Any) -> bool:
        return v is True or v is ...

    def __repr_args__(self) -> _repr.ReprArgs:
        return [(None, self._items)]


if typing.TYPE_CHECKING:

    def ClassAttribute(name: str, value: T) -> T:
        ...

else:

    class ClassAttribute:
        """
        Hide class attribute from its instances
        """

        __slots__ = 'name', 'value'

        def __init__(self, name: str, value: Any) -> None:
            self.name = name
            self.value = value

        def __get__(self, instance: Any, owner: type[Any]) -> None:
            if instance is None:
                return self.value
            raise AttributeError(f'{self.name!r} attribute of {owner.__name__!r} is class-only')


Obj = TypeVar('Obj')


def smart_deepcopy(obj: Obj) -> Obj:
    """
    Return type as is for immutable built-in types
    Use obj.copy() for built-in empty collections
    Use copy.deepcopy() for non-empty collections and unknown objects
    """

    obj_type = obj.__class__
    if obj_type in IMMUTABLE_NON_COLLECTIONS_TYPES:
        return obj  # fastest case: obj is immutable and not collection therefore will not be copied anyway
    try:
        if not obj and obj_type in BUILTIN_COLLECTIONS:
            # faster way for empty collections, no need to copy its members
            return obj if obj_type is tuple else obj.copy()  # type: ignore  # tuple doesn't have copy method
    except (TypeError, ValueError, RuntimeError):
        # do we really dare to catch ALL errors? Seems a bit risky
        pass

    return deepcopy(obj)  # slowest way when we actually might need a deepcopy


_EMPTY = object()


def all_identical(left: typing.Iterable[Any], right: typing.Iterable[Any]) -> bool:
    """
    Check that the items of `left` are the same objects as those in `right`.

    >>> a, b = object(), object()
    >>> all_identical([a, b, a], [a, b, a])
    True
    >>> all_identical([a, b, [a]], [a, b, [a]])  # new list object, while "equal" is not "identical"
    False
    """
    for left_item, right_item in zip_longest(left, right, fillvalue=_EMPTY):
        if left_item is not right_item:
            return False
    return True


if typing.TYPE_CHECKING:
    # define like this to work with older python
    KT = TypeVar('KT')
    VT = TypeVar('VT')

    # TODO: Remove this once we remove the `pydantic.generics` module.
    class LimitedDict(dict[KT, VT]):
        def __init__(self, size_limit: int = 1000):
            ...

else:

    class LimitedDict(dict):
        """
        Limit the size/length of a dict used for caching to avoid unlimited increase in memory usage.

        Since the dict is ordered, and we always remove elements from the beginning, this is effectively a FIFO cache.
        """

        def __init__(self, size_limit: int = 1000):
            self.size_limit = size_limit
            super().__init__()

        def __setitem__(self, __key: Any, __value: Any) -> None:
            super().__setitem__(__key, __value)
            if len(self) > self.size_limit:
                excess = len(self) - self.size_limit + self.size_limit // 10
                to_remove = list(self.keys())[:excess]
                for key in to_remove:
                    del self[key]

        def __class_getitem__(cls, *args: Any) -> Any:
            # to avoid errors with 3.7
            pass


def is_typed_dict_field(schema: CoreSchema | TypedDictField) -> typing.TypeGuard[TypedDictField]:
    return 'type' not in schema


def is_core_schema(schema: CoreSchema | TypedDictField) -> typing.TypeGuard[CoreSchema]:
    return 'type' in schema


def get_type_ref(type_: type[Any], args_override: tuple[type[Any], ...] | None = None) -> str:
    """
    Produces the ref to be used for this type by pydantic_core's core schemas.

    This `args_override` argument was added for the purpose of creating valid recursive references
    when creating generic models without needing to create a concrete class.
    """
    origin = getattr(type_, '__pydantic_generic_origin__', None) or type_
    args = getattr(type_, '__pydantic_generic_args__') or args_override or ()

    module_name = getattr(origin, '__module__', '<No __module__>')
    qualname = getattr(origin, '__qualname__', f'<No __qualname__: {origin}>')
    type_ref = f'{module_name}.{qualname}:{id(origin)}'

    arg_refs: list[str] = []
    for arg in args:
        arg_ref = f'{_repr.display_as_type(arg)}:{id(arg)}'
        arg_refs.append(arg_ref)
    if arg_refs:
        type_ref = f'{type_ref}[{",".join(arg_refs)}]'
    return type_ref
