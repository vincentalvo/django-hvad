from collections import defaultdict
import django
from django.conf import settings
from django.db import models, transaction, IntegrityError
from django.db.models.query import QuerySet, ValuesQuerySet, DateQuerySet
if django.VERSION >= (1, 6):
    from django.db.models.query import DateTimeQuerySet
try:
    from django.db.models.query import CHUNK_SIZE
except ImportError:
    CHUNK_SIZE = 100
from django.db.models import Q
from django.utils.translation import get_language
from hvad.fieldtranslator import translate
from hvad.query import q_children, where_node_children
from hvad.utils import combine, minimumDjangoVersion
from hvad.compat.settings import settings_updater
from copy import deepcopy
import logging
import sys
import warnings

#===============================================================================

# Logging-related globals
_logger = logging.getLogger(__name__)

# Global settings, wrapped so they react to SettingsOverride
@settings_updater
def update_settings(*args, **kwargs):
    global FALLBACK_LANGUAGES, LEGACY_FALLBACKS
    FALLBACK_LANGUAGES = tuple(code for code, name in settings.LANGUAGES)
    LEGACY_FALLBACKS = bool(getattr(settings, 'HVAD_LEGACY_FALLBACKS', django.VERSION < (1, 6)))

#===============================================================================

class FieldTranslator(object):
    """
    Translates *shared* field names from '<shared_field>' to
    'master__<shared_field>' and caches those names.
    """
    def __init__(self, manager):
        self._manager = manager
        self._shared_fields = tuple(manager.shared_model._meta.get_all_field_names()) + ('pk',)
        self._cache = dict()
        super(FieldTranslator, self).__init__()

    def __call__(self, key):
        try:
            ret = self._cache[key]
        except KeyError:
            ret = self._build(key)
            self._cache[key] = ret
        return ret

    def get(self, key):
        warnings.warn('FieldTranslator.get is deprecated, please directly call '
                      'the field translator: qs.field_translator(name).',
                      DeprecationWarning, stacklevel=2)
        return self(key)

    def _build(self, key):
        """
        Checks if the selected field is a shared field
        and in that case, prefixes it with master___
        It also handles - and ? in case its called by
        order_by()
        """
        if key == "?":
            return key
        if key.startswith("-"):
            prefix, key = "-", key[1:]
        else:
            prefix = ""
        if key.startswith(self._shared_fields):
            return '%smaster__%s' % (prefix, key)
        else:
            return '%s%s' % (prefix, key)

#===============================================================================

class ValuesMixin(object):
    _skip_master_select = True

    def iterator(self):
        qs = self._clone()._add_language_filter()
        for row in super(ValuesMixin, qs).iterator():
            if isinstance(row, dict):
                yield qs._reverse_translate_fieldnames_dict(row)
            else:
                yield row

class SkipMasterSelectMixin(object):
    _skip_master_select = True

#===============================================================================

class ForcedUniqueFields(object):
    """ Context manager that forces a set of fields to be unique while active """
    def __init__(self, fields):
        self.fields = fields

    def __enter__(self):
        for field in self.fields:
            field._unique = True

    def __exit__(self, *args):
        for field in self.fields:
            field._unique = False

#===============================================================================
# Field for language joins
#===============================================================================

class RawConstraint(object):
    def __init__(self, sql, aliases):
        self.sql = sql
        self.aliases = aliases

    def as_sql(self, qn, connection):
        aliases = tuple(qn(alias) for alias in self.aliases)
        return (self.sql % aliases, [])

class BetterTranslationsField(object):
    def __init__(self, translation_fallbacks):
        # Filter out duplicates, while preserving order
        self._fallbacks = []
        seen = set()
        for lang in translation_fallbacks:
            if lang not in seen:
                seen.add(lang)
                self._fallbacks.append(lang)

    def get_extra_restriction(self, where_class, alias, related_alias):
        langcase = ('(CASE %s.language_code ' +
                    ' '.join('WHEN \'%s\' THEN %d' % (lang, i)
                             for i, lang in enumerate(self._fallbacks)) +
                    ' ELSE %d END)' % len(self._fallbacks))
        return RawConstraint(
            sql=' '.join((langcase, '<', langcase, 'OR ('
                          '%s.language_code = %s.language_code AND '
                          '%s.id < %s.id)')),
            aliases=(alias, related_alias,
                     alias, related_alias,
                     alias, related_alias)
        )


#===============================================================================
# TranslationQueryset
#===============================================================================

class TranslationQueryset(QuerySet):
    """
    This is where things happen.
    To fully understand this project, you have to understand this class.
    Go through each method individually, maybe start with 'get', 'create' and
    'iterator'.

    IMPORTANT: the `model` attribute on this class is the *translated* Model,
    despite this being used as the queryset for the *shared* Model!
    """
    override_classes = {
        ValuesQuerySet: ValuesMixin,
        DateQuerySet: SkipMasterSelectMixin,
    }
    if django.VERSION >= (1, 6):
        override_classes[DateTimeQuerySet] = SkipMasterSelectMixin
    _skip_master_select = False

    def __init__(self, model, *args, **kwargs):
        if hasattr(model._meta, 'translations_model'):
            # normal creation gets a shared model that we must flip around
            model, self.shared_model = model._meta.translations_model, model
        elif not hasattr(model._meta, 'shared_model'):
            raise TypeError('TranslationQueryset only works on translatable models')
        self._local_field_names = None
        self._field_translator = None
        self._language_code = None
        self._language_fallbacks = None
        self._raw_select_related = []
        self._forced_unique_fields = []  # Used for select_related
        self._language_filter_tag = False
        self._hvad_switch_fields = ()
        super(TranslationQueryset, self).__init__(model, *args, **kwargs)

    #===========================================================================
    # Helpers and properties (INTERNAL!)
    #===========================================================================

    def _clone(self, klass=None, setup=False, **kwargs):
        """ Creates a clone of this queryset - Django equivalent of copy()
        This method keeps all defining attributes and drops data caches
        """
        kwargs.update({
            'shared_model': self.shared_model,
            '_local_field_names': self._local_field_names,
            '_field_translator': self._field_translator,
            '_language_code': self._language_code,
            '_language_fallbacks': self._language_fallbacks,
            '_raw_select_related': self._raw_select_related,
            '_forced_unique_fields': list(self._forced_unique_fields),
            '_language_filter_tag': getattr(self, '_language_filter_tag', False),
            '_hvad_switch_fields': self._hvad_switch_fields,
        })
        klass = self.__class__ if klass is None else self._get_class(klass)
        return super(TranslationQueryset, self)._clone(klass, setup, **kwargs)

    @property
    def field_translator(self):
        if self._field_translator is None:
            self._field_translator = FieldTranslator(self)
        return self._field_translator

    @property
    def shared_local_field_names(self):
        if self._local_field_names is None:
            self._local_field_names = self.shared_model._meta.get_all_field_names()
        return self._local_field_names

    def _translate_args_kwargs(self, *args, **kwargs):
        # Translate args (Q objects) from '<shared_field>' to
        # 'master__<shared_field>' where necessary.
        newargs = deepcopy(args)
        for q in newargs:
            for child, children, index in q_children(q):
                children[index] = (self.field_translator(child[0]), child[1])
        # Translated kwargs from '<shared_field>' to 'master__<shared_field>'
        # where necessary.
        newkwargs = dict((self.field_translator(key), value)
                         for key, value in kwargs.items())
        return newargs, newkwargs

    def _translate_fieldnames(self, fieldnames):
        return [self.field_translator(name) for name in fieldnames]

    def _reverse_translate_fieldnames_dict(self, fieldname_dict):
        """
        Helper function to make sure the user doesnt get "bothered"
        with the construction of shared/translated model

        Translates e.g.
        {'master__number_avg': 10} to {'number__avg': 10}

        """
        prefix = 'master__'
        return dict(
            (key[len(prefix):] if key.startswith(prefix) else key, value)
            for key, value in fieldname_dict.items()
        )

    def _split_kwargs(self, **kwargs):
        """
        Split kwargs into shared and translated fields
        """
        shared = {}
        translated = {}
        for key, value in kwargs.items():
            if key in self.shared_local_field_names:
                shared[key] = value
            else:
                translated[key] = value
        return shared, translated

    def _get_class(self, klass):
        for key, value in self.override_classes.items():
            if issubclass(klass, key):
                return type(value.__name__, (value, klass, TranslationQueryset,), {})
        return klass

    def _get_shared_queryset(self):
        qs = super(TranslationQueryset, self)._clone()
        qs.__class__ = QuerySet
        accessor = self.shared_model._meta.translations_accessor
        # update using the real manager
        return QuerySet(self.shared_model, using=self.db).filter(**{'%s__in' % accessor: qs})

    def _add_select_related(self, language_code):
        fields = self._raw_select_related
        related_queries = [] if self._skip_master_select else ['master']
        language_filters = []
        force_unique_fields = []

        for query_key in fields:
            bits = query_key.split('__')
            model = self.shared_model

            for depth, bit in enumerate(bits):
                # Resolve the field
                try:                             # lookup the field on the shared model
                    field, _, direct, _ = model._meta.get_field_by_name.real(bit)
                    translated = False
                except models.FieldDoesNotExist: # nope, that's from translations
                    field, _, direct, _ = (model._meta.translations_model
                                                ._meta.get_field_by_name(bit))
                    translated = True
                except AttributeError:           # model is not translatable anyway
                    field, _, direct, _ = model._meta.get_field_by_name(bit)
                    translated = False

                # Adjust current path bit
                if depth == 0 and not translated:
                    # on initial depth we must key to shared model
                    bits[depth] = 'master__%s' % bit
                elif depth > 0 and translated:
                    # on deeper levels we must key to translations model
                    # this will work because translations will be seen as _unique
                    # at query time
                    bits[depth] = '%s__%s' % (model._meta.translations_accessor, bit)


                # Find out target model
                if direct:  # field is on model
                    if field.rel:    # field is a foreign key, follow it
                        target = field.rel.to
                    else:            # field is a regular field
                        raise ValueError('Cannot select_related: %s is a regular field' % query_key)
                else:       # field is a m2m or reverse fk, follow it
                    target = field.model

                # If target is a translated model, select its translations
                if hasattr(target._meta, 'translations_accessor'):
                    # Add the model
                    target_query = '__'.join(bits[:depth+1])
                    related_queries.append('%s__%s' % (
                        target_query,
                        target._meta.translations_accessor
                    ))

                    # Add a language filter for the translation
                    target_translations = target._meta.translations_accessor
                    language_filters.append('%s__%s__language_code' % (
                        target_query,
                        target_translations,
                    ))

                    # Remember to mark the field unique so JOIN is generated
                    # and row decoder gets cached items
                    target_transfield = getattr(target, target_translations).related.field
                    force_unique_fields.append(target_transfield)

                model = target
            related_queries.append('__'.join(bits))

        # Apply results to query
        self.query.add_select_related(related_queries)
        for language_filter in language_filters:
            self.query.add_q(Q(**{language_filter: language_code}) |
                             Q(**{language_filter: None}))

        self._forced_unique_fields = force_unique_fields

    def _add_language_filter(self):
        if self._language_filter_tag:
            raise RuntimeError('Queryset is already tagged. This is a bug in hvad')
        self._language_filter_tag = True

        if self._language_code == 'all':
            if self._raw_select_related:
                raise NotImplementedError('Using select_related along with '
                                          'language(\'all\') is not supported')

            self.query.add_select_related(('master',))

        elif self._language_fallbacks:
            if self._raw_select_related:
                raise NotImplementedError('Using select_related along with '
                                          'fallbacks() is not supported')
            languages = tuple(get_language() if lang is None else lang
                              for lang in (self._language_code,) + self._language_fallbacks)

            masteratt = self.model._meta.get_field('master').attname
            nullable = ({'nullable': True} if django.VERSION >= (1, 7) else
                        {'nullable': True, 'outer_if_first': True})
            alias = self.query.join((self.query.get_initial_alias(), self.model._meta.db_table,
                                     ((masteratt, masteratt),)),
                                    join_field=BetterTranslationsField(languages),
                                    **nullable)
            self.query.add_extra(None, None, ('%s.id IS NULL'%alias,), None, None, None)
            self.query.add_filter(('master__pk__isnull', False))
            self.query.add_select_related(('master',))

        else:
            language_code = self._language_code or get_language()
            for _, field_name in where_node_children(self.query.where):
                if field_name == 'language_code':
                    warnings.warn(
                        'Overriding language_code in get() or filter() is deprecated. '
                        'Please set the language in Model.objects.language() instead, '
                        'or use language("all") to do manual filtering on languages.',
                        DeprecationWarning, stacklevel=3)
                    break
            else:
                self.query.add_filter(('language_code', language_code))
            self._add_select_related(language_code)

        # if queryset is about to use the model's default ordering, we
        # override that now with a translated version of the model's ordering
        if self.query.default_ordering and not self.query.order_by:
            ordering = self.shared_model._meta.ordering
            self.query.order_by = self._translate_fieldnames(ordering or [])

        return self

    def _use_related_translations(self, obj, relations_dict, depth=0):
        """
        Ensure that we use cached translations brought in via select_related if
        available. Necessary since the database select_related query caches the
        related translation models in a different place than hvad expects it.
        """

        # First, set translation for current object,
        try:
            accessor = obj._meta.translations_accessor
        except AttributeError:
            pass
        else:
            cache = getattr(obj.__class__, accessor).related.get_cache_name()
            try:
                translation = getattr(obj, cache)
            except AttributeError:
                pass
            else:
                delattr(obj, cache)
                setattr(obj, obj._meta.translations_cache, translation)

        # Then recurse in the relation dict
        for field, sub_dict in relations_dict.items():
            target = getattr(obj, field)
            if target is not None:
                self._use_related_translations(target, sub_dict, depth+1)


    #===========================================================================
    # Queryset/Manager API
    #===========================================================================

    def language(self, language_code=None):
        self._language_code = language_code
        return self

    @minimumDjangoVersion(1, 6)
    def fallbacks(self, *fallbacks):
        if not fallbacks:
            self._language_fallbacks = FALLBACK_LANGUAGES
        elif fallbacks == (None,):
            self._language_fallbacks = None
        else:
            self._language_fallbacks = fallbacks
        return self

    #===========================================================================
    # Queryset/Manager API that do database queries
    #===========================================================================

    def iterator(self):
        """
        If this queryset is not filtered by a language code yet, it should be
        filtered first by calling self.language.

        If someone doesn't want a queryset filtered by language, they should use
        Model.objects.untranslated()
        """
        qs = self._clone()._add_language_filter()
        qs._known_related_objects = {}  # super's iterator will attempt to set them

        if qs._forced_unique_fields:
            # HACK: In order for select_related to properly load data from
            # translated models, we have to force django to treat
            # certain fields as one-to-one relations
            # before this queryset calls get_cached_row()
            # We change it back so that things get reset to normal
            # before execution returns to user code.
            # It would be more direct and robust if we could wrap
            # django.db.models.query.get_cached_row() instead, but that's not a class
            # method, sadly, so we cannot override it just for this query

            with ForcedUniqueFields(qs._forced_unique_fields):
                # Pre-fetch all objects:
                objects = list(super(TranslationQueryset, qs).iterator())

            if type(qs.query.select_related) == dict:
                for obj in objects:
                    qs._use_related_translations(obj, qs.query.select_related)
        else:
            objects = super(TranslationQueryset, qs).iterator()

        for obj in objects:
            # non-cascade-deletion hack:
            if not obj.master:
                yield obj
            else:
                for name in self._hvad_switch_fields:
                    try:
                        setattr(obj.master, name, getattr(obj, name))
                    except AttributeError:
                        pass
                    else:
                        delattr(obj, name)
                obj = combine(obj, qs.shared_model)
                # use known objects from self, not qs as we cleared it earlier
                if django.VERSION >= (1, 6):
                    for field, rel_objs in self._known_related_objects.items():
                        if hasattr(obj, field.get_cache_name()):
                            continue # field was already cached
                        pk = getattr(obj, field.get_attname())
                        try:
                            rel_obj = rel_objs[pk]
                        except KeyError:
                            pass
                        else:
                            setattr(obj, field.name, rel_obj)
                else:
                    kro_attname, kro_instance = (getattr(self, 'known_related_object', None)
                                                 or (None, None))
                    if kro_instance:
                        setattr(obj, kro_attname, kro_instance)
                yield obj

    def create(self, **kwargs):
        if 'language_code' not in kwargs:
            kwargs['language_code'] = self._language_code or get_language()
        else:
            warnings.warn('Overriding language_code in create() is deprecated. '
                          'Please set the language in Model.objects.language() instead.',
                          DeprecationWarning, stacklevel=2)
        if kwargs['language_code'] == 'all':
            raise ValueError('Cannot create an object with language \'all\'')
        obj = self.shared_model(**kwargs)
        self._for_write = True
        obj.save(force_insert=True, using=self.db)
        return obj

    def count(self):
        if self._result_cache is None:
            qs = self._clone()._add_language_filter()
            return super(TranslationQueryset, qs).count()
        else:
            return len(self._result_cache)

    def exists(self):
        if self._result_cache is None:
            qs = self._clone()._add_language_filter()
            return super(TranslationQueryset, qs).exists()
        else:
            return bool(self._result_cache)

    def get_or_create(self, **kwargs):
        """
        Looks up an object with the given kwargs, creating one if necessary.
        Returns a tuple of (object, created), where created is a boolean
        specifying whether an object was created.
        """
        assert kwargs, \
                'get_or_create() must be passed at least one keyword argument'
        defaults = kwargs.pop('defaults', {})
        lookup = kwargs.copy()
        for f in self.model._meta.fields:
            if f.attname in lookup:
                lookup[f.name] = lookup.pop(f.attname)
        try:
            self._for_write = True
            return self.get(**lookup), False
        except self.model.DoesNotExist:
            try:
                params = dict([(k, v) for k, v in kwargs.items() if '__' not in k])
                params.update(defaults)
                # START PATCH
                if 'language_code' not in params:
                    params['language_code'] = self._language_code or get_language()
                else:
                    warnings.warn('Overriding language_code in get_or_create() is deprecated. '
                                  'Please set the language in Model.objects.language() instead.',
                                  DeprecationWarning, stacklevel=2)
                if params['language_code'] == 'all':
                    raise ValueError('Cannot create an object with language \'all\'')
                obj = self.shared_model(**params)
                # END PATCH
                sid = transaction.savepoint(using=self.db)
                obj.save(force_insert=True, using=self.db)
                transaction.savepoint_commit(sid, using=self.db)
                return obj, True
            except IntegrityError:
                transaction.savepoint_rollback(sid, using=self.db)
                exc_info = sys.exc_info()
                try:
                    return self.get(**lookup), False
                except self.model.DoesNotExist:
                    # Re-raise the IntegrityError with its original traceback.
                    raise exc_info[1]

    @minimumDjangoVersion(1, 7)
    def update_or_create(self, defaults=None, **kwargs):
        raise NotImplementedError()

    def bulk_create(self, objs, batch_size=None):
        raise NotImplementedError()

    def aggregate(self, *args, **kwargs):
        """
        Loops over all the passed aggregates and translates the fieldnames
        """
        qs = self._clone()._add_language_filter()
        newargs, newkwargs = [], {}
        for arg in args:
            arg.lookup = qs._translate_fieldnames([arg.lookup])[0]
            newargs.append(arg)
        for key in kwargs:
            value = kwargs[key]
            value.lookup = qs._translate_fieldnames([value.lookup])[0]
            newkwargs[key] = value
        response = super(TranslationQueryset, qs).aggregate(*newargs, **newkwargs)
        return qs._reverse_translate_fieldnames_dict(response)

    def latest(self, field_name=None):
        field_name = self.field_translator(field_name or self.shared_model._meta.get_latest_by)
        return super(TranslationQueryset, self).latest(field_name)

    @minimumDjangoVersion(1, 6)
    def earliest(self, field_name=None):
        field_name = self.field_translator(field_name or self.shared_model._meta.get_latest_by)
        return super(TranslationQueryset, self).earliest(field_name)

    def in_bulk(self, id_list):
        if not id_list:
            return {}
        if self._language_code == 'all':
            raise ValueError('Cannot use in_bulk along with language(\'all\').')
        qs = self.filter(pk__in=id_list)
        qs.query.clear_ordering(force_empty=True)
        return dict((obj._get_pk_val(), obj) for obj in qs.iterator())

    def delete(self):
        qs = self._get_shared_queryset()
        qs.delete()
    delete.alters_data = True
    delete.queryset_only = True

    def delete_translations(self):
        self.update(master=None)
        self.model.objects.filter(master__isnull=True).delete()
    delete_translations.alters_data = True

    def update(self, **kwargs):
        qs = self._clone()._add_language_filter()
        shared, translated = qs._split_kwargs(**kwargs)
        count = 0
        if translated:
            count += super(TranslationQueryset, qs).update(**translated)
        if shared:
            shared_qs = qs._get_shared_queryset()
            count += shared_qs.update(**shared)
        return count
    update.alters_data = True

    #===========================================================================
    # Queryset/Manager API that return another queryset
    #===========================================================================

    def filter(self, *args, **kwargs):
        if 'language_code' in kwargs and kwargs['language_code'] == 'all':
            raise ValueError('Value "all" is invalid for language_code')
        newargs, newkwargs = self._translate_args_kwargs(*args, **kwargs)
        return super(TranslationQueryset, self).filter(*newargs, **newkwargs)

    def exclude(self, *args, **kwargs):
        if 'language_code' in kwargs and kwargs['language_code'] == 'all':
            raise ValueError('Value "all" is invalid for language_code')
        newargs, newkwargs = self._translate_args_kwargs(*args, **kwargs)
        return super(TranslationQueryset, self).exclude(*newargs, **newkwargs)

    def extra(self, select=None, where=None, params=None, tables=None,
              order_by=None, select_params=None):
        qs = super(TranslationQueryset, self).extra(select, where, params, tables,
                                                    order_by, select_params)
        if select:
            switch_fields = set(self._hvad_switch_fields)
            switch_fields.update(select.keys())
            qs._hvad_switch_fields = tuple(switch_fields)
        return qs

    def values(self, *fields):
        fields = self._translate_fieldnames(fields)
        return super(TranslationQueryset, self).values(*fields)

    def values_list(self, *fields, **kwargs):
        fields = self._translate_fieldnames(fields)
        return super(TranslationQueryset, self).values_list(*fields, **kwargs)

    def dates(self, field_name, kind=None, order='ASC'):
        field_name = self.field_translator(field_name)
        return super(TranslationQueryset, self).dates(field_name, kind=kind, order=order)

    @minimumDjangoVersion(1, 6)
    def datetimes(self, field, *args, **kwargs):
        field = self.field_translator(field)
        return super(TranslationQueryset, self).datetimes(field, *args, **kwargs)

    def select_related(self, *fields):
        if not fields:
            raise NotImplementedError('To use select_related on a translated model, '
                                      'you must provide a list of fields.')
        if fields == (None,):
            self._raw_select_related = []
        elif django.VERSION >= (1, 7):  # in newer versions, calls are cumulative
            self._raw_select_related.extend(fields)
        else:                           # in older versions, they overwrite each other
            self._raw_select_related = list(fields)
        return self

    def complex_filter(self, filter_obj):
        # Don't know how to handle Q object yet, but it is probably doable...
        # An unknown type object that supports 'add_to_query' is a different story :)
        if isinstance(filter_obj, models.Q) or hasattr(filter_obj, 'add_to_query'):
            raise NotImplementedError()

        newargs, newkwargs = self._translate_args_kwargs(**filter_obj)
        return super(TranslationQueryset, self)._filter_or_exclude(None, *newargs, **newkwargs)

    def annotate(self, *args, **kwargs):
        raise NotImplementedError()

    def order_by(self, *field_names):
        """
        Returns a new QuerySet instance with the ordering changed.
        """
        fieldnames = self._translate_fieldnames(field_names)
        return super(TranslationQueryset, self).order_by(*fieldnames)

    def reverse(self):
        return super(TranslationQueryset, self).reverse()

    def defer(self, *fields):
        raise NotImplementedError()

    def only(self, *fields):
        raise NotImplementedError()

#===============================================================================
# Fallbacks
#===============================================================================

class _SharedFallbackQueryset(QuerySet):
    translation_fallbacks = None

    def use_fallbacks(self, *fallbacks):
        self.translation_fallbacks = fallbacks or (None,)+FALLBACK_LANGUAGES
        return self

    def _clone(self, klass=None, setup=False, **kwargs):
        kwargs.update({
            'translation_fallbacks': self.translation_fallbacks,
        })
        return super(_SharedFallbackQueryset, self)._clone(klass, setup, **kwargs)

    def aggregate(self, *args, **kwargs):
        raise NotImplementedError()

    def annotate(self, *args, **kwargs):
        raise NotImplementedError()

    def defer(self, *args, **kwargs):
        raise NotImplementedError()

    def only(self, *args, **kwargs):
        raise NotImplementedError()


class LegacyFallbackQueryset(_SharedFallbackQueryset):
    '''
    Queryset that tries to load a translated version using fallbacks on a per
    instance basis.
    BEWARE: creates a lot of queries!
    '''
    def _get_real_instances(self, base_results):
        """
        The logic for this method was taken from django-polymorphic by Bert
        Constantin (https://github.com/bconstantin/django_polymorphic) and was
        slightly altered to fit the needs of django-hvad.
        """
        # get the primary keys of the shared model results
        base_ids = [obj.pk for obj in base_results]
        fallbacks = [get_language() if lang is None else lang
                     for lang in self.translation_fallbacks]
        # get all translations for the fallbacks chosen for those shared models,
        # note that this query is *BIG* and might return a lot of data, but it's
        # arguably faster than running one query for each result or even worse
        # one query per result per language until we find something
        translations_manager = self.model._meta.translations_model.objects
        baseqs = translations_manager.select_related('master')
        translations = baseqs.filter(language_code__in=fallbacks,
                                     master__pk__in=base_ids)
        fallback_objects = defaultdict(dict)
        # turn the results into a dict of dicts with shared model primary key as
        # keys for the first dict and language codes for the second dict
        for obj in translations:
            fallback_objects[obj.master.pk][obj.language_code] = obj
        # iterate over the share dmodel results
        for instance in base_results:
            translation = None
            # find the translation
            for fallback in fallbacks:
                translation = fallback_objects[instance.pk].get(fallback, None)
                if translation is not None:
                    break
            # if we found a translation, yield the combined result
            if translation:
                yield combine(translation, self.model)
            else:
                # otherwise yield the shared instance only
                _logger.error("no translation for %s.%s (pk=%s)" %
                              (instance._meta.app_label,
                               instance.__class__.__name__,
                               str(instance.pk)))
                yield instance

    def iterator(self):
        """
        The logic for this method was taken from django-polymorphic by Bert
        Constantin (https://github.com/bconstantin/django_polymorphic) and was
        slightly altered to fit the needs of django-hvad.
        """
        base_iter = super(LegacyFallbackQueryset, self).iterator()

        # only do special stuff when we actually want fallbacks
        if self.translation_fallbacks:
            while True:
                base_result_objects = []
                reached_end = False

                # get the next "chunk" of results
                for i in range(CHUNK_SIZE):
                    try:
                        instance = next(base_iter)
                        base_result_objects.append(instance)
                    except StopIteration:
                        reached_end = True
                        break

                # "combine" the results with their fallbacks
                real_results = self._get_real_instances(base_result_objects)

                # yield em!
                for instance in real_results:
                    yield instance

                # get out of the while loop if we're at the end, since this is
                # an iterator, we need to raise StopIteration, not "return".
                if reached_end:
                    raise StopIteration
        else:
            # just iterate over it
            for instance in base_iter:
                yield instance

class SelfJoinFallbackQueryset(_SharedFallbackQueryset):
    def iterator(self):
        # only do special stuff when we actually want fallbacks
        if self.translation_fallbacks:
            fallbacks = [get_language() if lang is None else lang
                         for lang in self.translation_fallbacks]
            tmodel = self.model._meta.translations_model
            taccessor = self.model._meta.translations_accessor
            taccessorcache = getattr(self.model, taccessor).related.get_cache_name()
            tcache = self.model._meta.translations_cache
            masteratt = tmodel._meta.get_field('master').attname
            field = BetterTranslationsField(fallbacks)

            qs = self._clone()

            qs.query.add_select_related((taccessor,))
            # This join will be reused by the select_related. We must provide it
            # anyway because the order matters and add_select_related does not
            # populate joins right away.
            nullable = ({'nullable': True} if django.VERSION >= (1, 7) else
                        {'nullable': True, 'outer_if_first': True})
            alias1 = qs.query.join((qs.query.get_initial_alias(), tmodel._meta.db_table,
                                    ((qs.model._meta.pk.attname, masteratt),)),
                                   join_field=getattr(qs.model, taccessor).related.field.rel,
                                   **nullable)
            alias2 = qs.query.join((tmodel._meta.db_table, tmodel._meta.db_table,
                                    ((masteratt, masteratt),)),
                                   join_field=field, **nullable)
            qs.query.add_extra(None, None, ('%s.id IS NULL'%alias2,), None, None, None)

            # We must force the _unique field so get_cached_row populates the cache
            # Unfortunately, this means we must load everything in one go
            getattr(qs.model, taccessor).related.field._unique = True
            objects = []
            for instance in super(SelfJoinFallbackQueryset, qs).iterator():
                try:
                    translation = getattr(instance, taccessorcache)
                except AttributeError:
                    _logger.error("no translation for %s.%s (pk=%s)",
                                  instance._meta.app_label,
                                  instance.__class__.__name__,
                                  str(instance.pk))
                else:
                    setattr(instance, tcache, translation)
                    delattr(instance, taccessorcache)
                objects.append(instance)
            getattr(qs.model, taccessor).related.field._unique = False
            return iter(objects)
        else:
            return super(SelfJoinFallbackQueryset, self).iterator()


FallbackQueryset = LegacyFallbackQueryset if LEGACY_FALLBACKS else SelfJoinFallbackQueryset


class TranslationFallbackManager(models.Manager):
    """
    Manager class for the shared model, without specific translations. Allows
    using `use_fallbacks()` to enable per object language fallback.
    """
    def __init__(self, *args, **kwargs):
        warnings.warn('TranslationFallbackManager is deprecated. Please use '
                      'TranslationManager\'s untranslated() method.',
                      DeprecationWarning, stacklevel=2)
        super(TranslationFallbackManager, self).__init__(*args, **kwargs)

    def use_fallbacks(self, *fallbacks):
        if django.VERSION >= (1, 6):
            return self.get_queryset().use_fallbacks(*fallbacks)
        else:
            return self.get_query_set().use_fallbacks(*fallbacks)

    def get_queryset(self):
        return FallbackQueryset(self.model, using=self.db)
    get_query_set = get_queryset        # old name for Django < 1.6


#===============================================================================
# TranslationManager
#===============================================================================


class TranslationManager(models.Manager):
    """
    Manager class for models with translated fields
    """
    #===========================================================================
    # API
    #===========================================================================
    use_for_related_fields = True

    queryset_class = TranslationQueryset
    fallback_class = FallbackQueryset
    default_class = QuerySet

    def __init__(self, *args, **kwargs):
        self.queryset_class = kwargs.pop('queryset_class', self.queryset_class)
        self.fallback_class = kwargs.pop('fallback_class', self.fallback_class)
        self.default_class = kwargs.pop('default_class', self.default_class)
        super(TranslationManager, self).__init__(*args, **kwargs)

    def using_translations(self):
        warnings.warn('using_translations() is deprecated, use language() instead',
                      DeprecationWarning, stacklevel=2)
        qs = self.queryset_class(self.model, using=self.db)
        if hasattr(self, 'core_filters'):
            qs = qs._next_is_sticky().filter(**(self.core_filters))
        return qs

    def _make_queryset(self, klass, core_filters):
        ''' Builds a queryset of given class.
            core_filters tells whether the queryset will bypass RelatedManager
            mechanics and therefore needs to reapply the filters on its own.
        '''
        if django.VERSION >= (1, 7):
            qs = klass(self.model, using=self.db, hints=self._hints)
        else:
            qs = klass(self.model, using=self.db)
        core_filters = getattr(self, 'core_filters', None) if core_filters else None
        if core_filters:
            qs = qs._next_is_sticky().filter(**core_filters)
        return qs

    def language(self, language_code=None):
        return self._make_queryset(self.queryset_class, True).language(language_code)

    def untranslated(self):
        return self._make_queryset(self.fallback_class, True)

    def get_queryset(self):
        return self._make_queryset(self.default_class, False)
    get_query_set = get_queryset        # old name for Django < 1.6

    #===========================================================================
    # Internals
    #===========================================================================

    @property
    def translations_model(self):
        return self.model._meta.translations_model


#===============================================================================
# TranslationAware
#===============================================================================


class TranslationAwareQueryset(QuerySet):
    def __init__(self, *args, **kwargs):
        super(TranslationAwareQueryset, self).__init__(*args, **kwargs)
        self._language_code = None

    def _translate(self, key, model, language_joins):
        ''' Translate a key on a model.
            language_joins must be a set that will be updated with required
            language joins for given key
        '''
        newkey, joins = translate(key, model)
        language_joins.update(joins)
        return newkey

    def _translate_args_kwargs(self, *args, **kwargs):
        self.language(self._language_code)
        language_joins = set()
        extra_filters = Q()
        newkwargs = dict(
            (self._translate(key, self.model, language_joins), value)
            for key, value in kwargs.items()
        )
        newargs = deepcopy(args)
        for q in newargs:
            for child, children, index in q_children(q):
                children[index] = (self._translate(child[0], self.model, language_joins),
                                   child[1])
        for langjoin in language_joins:
            extra_filters &= Q(**{langjoin: self._language_code})
        return newargs, newkwargs, extra_filters

    def _translate_fieldnames(self, fields):
        self.language(self._language_code)
        extra_filters = Q()
        language_joins = set()
        newfields = tuple(
            self._translate(field, self.model, language_joins)
            for field in fields
        )

        for langjoin in language_joins:
            extra_filters &= Q(**{langjoin: self._language_code})
        return newfields, extra_filters

    #===========================================================================
    # Queryset/Manager API
    #===========================================================================

    def language(self, language_code=None):
        if not language_code:
            language_code = get_language()
        self._language_code = language_code
        return self

    def get(self, *args, **kwargs):
        newargs, newkwargs, extra_filters = self._translate_args_kwargs(*args, **kwargs)
        return self._filter_extra(extra_filters).get(*newargs, **newkwargs)

    def filter(self, *args, **kwargs):
        newargs, newkwargs, extra_filters = self._translate_args_kwargs(*args, **kwargs)
        return self._filter_extra(extra_filters).filter(*newargs, **newkwargs)

    def aggregate(self, *args, **kwargs):
        raise NotImplementedError()

    def latest(self, field_name=None):
        extra = Q()
        if field_name:
            language_joins = set()
            field_name = self._translate(self, field_name, language_joins)
            for join in language_joins:
                extra &= Q(**{join: self._language_code})
        return self._filter_extra(extra).latest(field_name)

    @minimumDjangoVersion(1, 6)
    def earliest(self, field_name=None):
        extra = Q()
        if field_name:
            language_joins = set()
            field_name = self._translate(self, field_name, language_joins)
            for join in language_joins:
                extra &= Q(**{join: self._language_code})
        return self._filter_extra(extra).earliest(field_name)

    def in_bulk(self, id_list):
        if not id_list:
            return {}
        qs = self.filter(pk__in=id_list)
        qs.query.clear_ordering(force_empty=True)
        return dict((obj._get_pk_val(), obj) for obj in qs.iterator())

    def values(self, *fields):
        fields, extra_filters = self._translate_fieldnames(fields)
        return self._filter_extra(extra_filters).values(*fields)

    def values_list(self, *fields, **kwargs):
        fields, extra_filters = self._translate_fieldnames(fields)
        return self._filter_extra(extra_filters).values_list(*fields, **kwargs)

    def dates(self, field_name, kind, order='ASC'):
        raise NotImplementedError()

    @minimumDjangoVersion(1, 6)
    def datetimes(self, field, *args, **kwargs):
        raise NotImplementedError()

    def exclude(self, *args, **kwargs):
        newargs, newkwargs, extra_filters = self._translate_args_kwargs(*args, **kwargs)
        return self._exclude_extra(extra_filters).exclude(*newargs, **newkwargs)

    def complex_filter(self, filter_obj):
        # admin calls this with an empy filter_obj sometimes
        if filter_obj == {}:
            return self
        raise NotImplementedError()

    def annotate(self, *args, **kwargs):
        raise NotImplementedError()

    def order_by(self, *field_names):
        """
        Returns a new QuerySet instance with the ordering changed.
        """
        fieldnames, extra_filters = self._translate_fieldnames(field_names)
        return self._filter_extra(extra_filters).order_by(*fieldnames)

    def reverse(self):
        raise NotImplementedError()

    def defer(self, *fields):
        raise NotImplementedError()

    def only(self, *fields):
        raise NotImplementedError()

    def _clone(self, klass=None, setup=False, **kwargs):
        kwargs.update({
            '_language_code': self._language_code,
        })
        return super(TranslationAwareQueryset, self)._clone(klass, setup, **kwargs)

    def _filter_extra(self, extra_filters):
        qs = super(TranslationAwareQueryset, self).filter(extra_filters)
        return super(TranslationAwareQueryset, qs)

    def _exclude_extra(self, extra_filters):
        qs = super(TranslationAwareQueryset, self).exclude(extra_filters)
        return super(TranslationAwareQueryset, qs)


class TranslationAwareManager(models.Manager):
    def language(self, language_code=None):
        if django.VERSION >= (1, 6):
            return self.get_queryset().language(language_code)
        else:
            return self.get_query_set().language(language_code)

    def get_queryset(self):
        qs = TranslationAwareQueryset(self.model, using=self.db)
        return qs
    get_query_set = get_queryset        # old name for Django < 1.6


#===============================================================================
# Translations Model Manager
#===============================================================================


class TranslationsModelManager(models.Manager):
    def get_language(self, language):
        return self.get(language_code=language)
