from __future__ import annotations

import sys
import types
import typing
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from types import prepare_class
from typing import TYPE_CHECKING, Any, Generic, Iterator, List, Mapping, Tuple, Type, TypeVar
from weakref import WeakValueDictionary

import typing_extensions
from pydantic_core import core_schema

from ._self_type import BaseSelfType, get_self_type
from ._typing_extra import TypeVarType, typing_base
from ._utils import all_identical, get_type_ref, lenient_issubclass

if sys.version_info >= (3, 10):
    from typing import _UnionGenericAlias  # type: ignore[attr-defined]

if TYPE_CHECKING:
    from pydantic import BaseModel

GenericTypesCacheKey = Tuple[Type[Any], Any, Tuple[Any, ...]]

# weak dictionaries allow the dynamically created parametrized versions of generic models to get collected
# once they are no longer referenced by the caller.
if sys.version_info >= (3, 9):  # Typing for weak dictionaries available at 3.9
    GenericTypesCache = WeakValueDictionary[GenericTypesCacheKey, 'Type[BaseModel]']
else:
    GenericTypesCache = WeakValueDictionary


def create_generic_submodel(model_name: str, origin: type[BaseModel], args: tuple[Any, ...]) -> type[BaseModel]:
    """
    Dynamically create a submodel of a provided (generic) BaseModel.

    This is used when producing concrete parametrizations of generic models. This function
    only *creates* the new subclass; the schema/validators/serialization must be updated to
    reflect a concrete parametrization elsewhere.

    :param model_name: name of the newly created model
    :param origin: base class for the new model to inherit from
    """
    namespace: dict[str, Any] = {'__module__': origin.__module__}
    bases = (origin,)
    meta, ns, kwds = prepare_class(model_name, bases)
    namespace.update(ns)
    created_model = meta(
        model_name,
        bases,
        namespace,
        __pydantic_generic_origin__=origin,
        __pydantic_generic_args__=args,
        **kwds,
    )

    model_module, called_globally = _get_caller_frame_info(depth=3)
    if called_globally:  # create global reference and therefore allow pickling
        object_by_reference = None
        reference_name = model_name
        reference_module_globals = sys.modules[created_model.__module__].__dict__
        while object_by_reference is not created_model:
            object_by_reference = reference_module_globals.setdefault(reference_name, created_model)
            reference_name += '_'

    return created_model


def _get_caller_frame_info(depth: int = 2) -> tuple[str | None, bool]:
    """
    Used inside a function to check whether it was called globally

    :returns Tuple[module_name, called_globally]
    """
    try:
        previous_caller_frame = sys._getframe(depth)
    except ValueError as e:
        raise RuntimeError('This function must be used inside another function') from e
    except AttributeError:  # sys module does not have _getframe function, so there's nothing we can do about it
        return None, False
    frame_globals = previous_caller_frame.f_globals
    return frame_globals.get('__name__'), previous_caller_frame.f_locals is frame_globals


def is_generic_model(model: type[BaseModel]) -> bool:
    return issubclass(model, Generic)  # type: ignore[arg-type]


DictValues: type[Any] = {}.values().__class__


def iter_contained_typevars(v: Any) -> Iterator[TypeVarType]:
    """
    Recursively iterate through all subtypes and type args of `v` and yield any typevars that are found.

    This is meant as an alternative to directly accessing the `__parameters__` attribute of a GenericAlias,
    since __parameters__ of (nested) custom classes won't show up in that list.
    """
    from pydantic import BaseModel

    if isinstance(v, TypeVar):
        yield v
    elif (
        # TODO: I think we can/should replace __parameters__ with __pydantic_generic_parameters__
        hasattr(v, '__parameters__')
        and lenient_issubclass(v, BaseModel)
    ):
        yield from v.__parameters__ or ()
    elif isinstance(v, (DictValues, list)):
        for var in v:
            yield from iter_contained_typevars(var)
    else:
        args = get_args(v)
        for arg in args:
            yield from iter_contained_typevars(arg)


def get_args(v: Any) -> Any:
    pydantic_generic_args = getattr(v, '__pydantic_generic_args__', None)
    if pydantic_generic_args:
        return pydantic_generic_args
    return typing_extensions.get_args(v)


def get_origin(v: Any) -> Any:
    pydantic_generic_origin = getattr(v, '__pydantic_generic_origin__', None)
    if pydantic_generic_origin:
        return pydantic_generic_origin
    return typing_extensions.get_origin(v)


def replace_types(type_: Any, type_map: Mapping[Any, Any]) -> Any:
    """Return type with all occurrences of `type_map` keys recursively replaced with their values.

    :param type_: Any type, class or generic alias
    :param type_map: Mapping from `TypeVar` instance to concrete types.
    :return: New type representing the basic structure of `type_` with all
        `typevar_map` keys recursively replaced.

    >>> replace_types(Tuple[str, Union[List[str], float]], {str: int})
    Tuple[int, Union[List[int], float]]

    """
    if not type_map:
        return type_

    type_args = get_args(type_)
    origin_type = get_origin(type_)

    if origin_type is typing_extensions.Annotated:
        annotated_type, *annotations = type_args
        annotated = replace_types(annotated_type, type_map)
        for annotation in annotations:
            annotated = typing_extensions.Annotated[annotated, annotation]
        return annotated

    # Having type args is a good indicator that this is a typing module
    # class instantiation or a generic alias of some sort.
    if type_args:
        resolved_type_args = tuple(replace_types(arg, type_map) for arg in type_args)
        if all_identical(type_args, resolved_type_args):
            # If all arguments are the same, there is no need to modify the
            # type or create a new object at all
            return type_
        if (
            origin_type is not None
            and isinstance(type_, typing_base)
            and not isinstance(origin_type, typing_base)
            and getattr(type_, '_name', None) is not None
        ):
            # In python < 3.9 generic aliases don't exist so any of these like `list`,
            # `type` or `collections.abc.Callable` need to be translated.
            # See: https://www.python.org/dev/peps/pep-0585
            origin_type = getattr(typing, type_._name)
        assert origin_type is not None
        # PEP-604 syntax (Ex.: list | str) is represented with a types.UnionType object that does not have __getitem__.
        # We also cannot use isinstance() since we have to compare types.
        if sys.version_info >= (3, 10) and origin_type is types.UnionType:  # noqa: E721
            return _UnionGenericAlias(origin_type, resolved_type_args)
        return origin_type[resolved_type_args]

    # We handle pydantic generic models separately as they don't have the same
    # semantics as "typing" classes or generic aliases
    from pydantic import BaseModel

    if not origin_type and lenient_issubclass(type_, BaseModel) and getattr(type_, '__parameters__', None):
        parameters = type_.__parameters__
        resolved_type_args = tuple(replace_types(t, type_map) for t in parameters)
        if all_identical(parameters, resolved_type_args):
            return type_
        return type_[resolved_type_args]

    # Handle special case for typehints that can have lists as arguments.
    # `typing.Callable[[int, str], int]` is an example for this.
    if isinstance(type_, (List, list)):
        resolved_list = list(replace_types(element, type_map) for element in type_)
        if all_identical(type_, resolved_list):
            return type_
        return resolved_list

    if lenient_issubclass(type_, BaseSelfType):
        # queue the replacement as a deferred action
        return type_.replace_types(type_map)

    # If all else fails, we try to resolve the type directly and otherwise just
    # return the input with no modifications.
    return type_map.get(type_, type_)


def check_parameters_count(cls: type[BaseModel], parameters: tuple[Any, ...]) -> None:
    actual = len(parameters)
    expected = len(getattr(cls, '__parameters__', ()))
    if actual != expected:
        description = 'many' if actual > expected else 'few'
        raise TypeError(f'Too {description} parameters for {cls}; actual {actual}, expected {expected}')


_visit_counts_context: ContextVar[dict[str, int] | None] = ContextVar('_visit_counts_context', default=None)


@contextmanager
def generic_recursion_self_type(origin: type[BaseModel], args: tuple[Any, ...]) -> Iterator[type[BaseSelfType] | None]:
    """
    This contextmanager should be placed around recursive calls used to build a generic type,
    and accept as arguments the generic origin type and the type arguments being passed to it.

    If the same origin and arguments are observed twice, it implies that a self-reference placeholder
    can be used while building the core schema, and will produce a schema_ref that will be valid in the
    final parent schema.

    I believe the main reason that the same origin/args must be observed twice is that a BaseModel's
    inner_schema will be a TypedDictSchema that doesn't include the first occurrence of the SelfType
    reference, so the referenced schema may not end up in the final core_schema unless you expand two
    layers deep.
    """
    visit_counts_by_ref = _visit_counts_context.get()
    if visit_counts_by_ref is None:
        visit_counts_by_ref = defaultdict(int)
        token = _visit_counts_context.set(visit_counts_by_ref)
    else:
        token = None

    type_ref = get_type_ref(origin, args_override=args)
    if visit_counts_by_ref[type_ref] >= 2:
        self_type = get_self_type(
            core_schema.definition_reference_schema(type_ref), origin, [{'kind': 'class_getitem', 'item': args}]
        )
        yield self_type
    else:
        visit_counts_by_ref[type_ref] += 1
        yield None
    if token:
        _visit_counts_context.reset(token)


def recursively_defined_type_refs() -> set[str]:
    return set((_visit_counts_context.get() or {}).keys())


_GENERIC_TYPES_CACHE = GenericTypesCache()


def _cache_key(cls: type[BaseModel], _params: Any) -> GenericTypesCacheKey:
    # TODO: What is happening with args here?
    #   Note: get_args always returns () if _params is a tuple, and
    #   if _params is a type, _params and (_params,) should have the same meaning as arguments to __class_getitem__.
    #   So something seems suspicious..
    args = typing_extensions.get_args(_params)
    # python returns a list for Callables, which is not hashable
    if len(args) == 2 and isinstance(args[0], list):
        args = (tuple(args[0]), args[1])
    return cls, _params, args


def set_cached_generic_type(parent: type[BaseModel], typevar_values: tuple[Any, ...], type_: type[BaseModel]) -> None:
    _GENERIC_TYPES_CACHE[_cache_key(parent, typevar_values)] = type_
    if len(typevar_values) == 1:
        _GENERIC_TYPES_CACHE[_cache_key(parent, typevar_values[0])] = type_


def get_cached_generic_type(parent: type[BaseModel], typevar_values: Any) -> type[BaseModel] | None:
    return _GENERIC_TYPES_CACHE.get(_cache_key(parent, typevar_values))
