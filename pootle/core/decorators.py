# -*- coding: utf-8 -*-
#
# Copyright (C) Pootle contributors.
#
# This file is a part of the Pootle project. It is distributed under the GPL3
# or later license. See the LICENSE file for a copy of the license and the
# AUTHORS file for copyright and authorship information.

from functools import wraps

from django.core.exceptions import PermissionDenied
from django.core.urlresolvers import reverse
from django.db import connection
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect

from pootle.i18n.gettext import ugettext as _
from pootle_app.models.directory import Directory
from pootle_app.models.permissions import (check_permission,
                                           get_matching_permissions)
from pootle_language.models import Language
from pootle_project.models import Project, ProjectResource, ProjectSet
from pootle_store.models import Store
from pootle_translationproject.models import TranslationProject

from .cache import get_cache
from .exceptions import Http400
from .url_helpers import split_pootle_path


CLS2ATTR = {
    'TranslationProject': 'translation_project',
    'Project': 'project',
    'Language': 'language',
}


def get_path_obj(func):
    @wraps(func)
    def wrapped(request, *args, **kwargs):
        if request.is_ajax():
            pootle_path = request.GET.get('path', None)
            if pootle_path is None:
                raise Http400(_('Arguments missing.'))

            language_code, project_code, dir_path, filename = \
                split_pootle_path(pootle_path)
            kwargs['dir_path'] = dir_path
            kwargs['filename'] = filename

            # Remove potentially present but unwanted args
            try:
                del kwargs['language_code']
                del kwargs['project_code']
            except KeyError:
                pass
        else:
            language_code = kwargs.pop('language_code', None)
            project_code = kwargs.pop('project_code', None)

        if language_code and project_code:
            try:
                path_obj = TranslationProject.objects.get_for_user(
                    user=request.user,
                    language_code=language_code,
                    project_code=project_code,
                )
            except TranslationProject.DoesNotExist:
                path_obj = None

            if path_obj is None:
                if not request.is_ajax():
                    # Explicit selection via the UI: redirect either to
                    # ``/language_code/`` or ``/projects/project_code/``
                    user_choice = request.COOKIES.get('user-choice', None)
                    if user_choice and user_choice in ('language', 'project',):
                        url = {
                            'language': reverse('pootle-language-browse',
                                                args=[language_code]),
                            'project': reverse('pootle-project-browse',
                                               args=[project_code, '', '']),
                        }
                        response = redirect(url[user_choice])
                        response.delete_cookie('user-choice')

                        return response

                raise Http404
        elif language_code:
            user_projects = Project.accessible_by_user(request.user)
            language = get_object_or_404(Language, code=language_code)
            children = language.children \
                               .filter(project__code__in=user_projects)
            language.set_children(children)
            path_obj = language
        elif project_code:
            try:
                path_obj = Project.objects.get_for_user(project_code,
                                                        request.user)
            except Project.DoesNotExist:
                raise Http404
        else:  # No arguments: all user-accessible projects
            user_projects = Project.objects.for_user(request.user)
            path_obj = ProjectSet(user_projects)

        request.ctx_obj = path_obj
        request.ctx_path = path_obj.pootle_path
        request.resource_obj = path_obj
        request.pootle_path = path_obj.pootle_path

        return func(request, path_obj, *args, **kwargs)

    return wrapped


def set_resource(request, path_obj, dir_path, filename):
    """Loads :cls:`pootle_app.models.Directory` and
    :cls:`pootle_store.models.Store` models and populates the
    request object.

    :param path_obj: A path-like object object.
    :param dir_path: Path relative to the root of `path_obj`.
    :param filename: Optional filename.
    """
    obj_directory = getattr(path_obj, 'directory', path_obj)
    ctx_path = obj_directory.pootle_path
    resource_path = dir_path
    pootle_path = ctx_path + dir_path

    directory = None
    store = None

    is_404 = False

    if filename:
        pootle_path = pootle_path + filename
        resource_path = resource_path + filename

        try:
            store = Store.objects.live().select_related(
                'translation_project',
                'parent',
            ).get(pootle_path=pootle_path)
            directory = store.parent
        except Store.DoesNotExist:
            is_404 = True

    if directory is None and not is_404:
        if dir_path:
            try:
                directory = Directory.objects.live().get(
                    pootle_path=pootle_path)
            except Directory.DoesNotExist:
                is_404 = True
        else:
            directory = obj_directory

    if is_404:  # Try parent directory
        language_code, project_code = split_pootle_path(pootle_path)[:2]
        if not filename:
            dir_path = dir_path[:dir_path[:-1].rfind('/') + 1]

        url = reverse('pootle-tp-browse',
                      args=[language_code, project_code, dir_path])
        request.redirect_url = url

        raise Http404

    request.store = store
    request.directory = directory
    request.pootle_path = pootle_path

    request.resource_obj = store or (directory if dir_path else path_obj)
    request.resource_path = resource_path
    request.ctx_obj = path_obj or request.resource_obj
    request.ctx_path = ctx_path


def set_project_resource(request, path_obj, dir_path, filename):
    """Loads :cls:`pootle_app.models.Directory` and
    :cls:`pootle_store.models.Store` models and populates the
    request object.

    This is the same as `set_resource` but operates at the project level
    across all languages.

    :param path_obj: A :cls:`pootle_project.models.Project` object.
    :param dir_path: Path relative to the root of `path_obj`.
    :param filename: Optional filename.
    """
    query_ctx_path = ''.join(['/%/', path_obj.code, '/'])
    query_pootle_path = query_ctx_path + dir_path

    obj_directory = getattr(path_obj, 'directory', path_obj)
    ctx_path = obj_directory.pootle_path
    resource_path = dir_path
    pootle_path = ctx_path + dir_path

    # List of TP paths available for user
    user_tps = TranslationProject.objects.for_user(request.user)
    user_tps = user_tps.filter(
        project__code=path_obj.code,
    ).values_list('pootle_path', flat=True)
    user_tps = list(path for path in user_tps
                    if not path.startswith('/templates/'))
    user_tps_regex = '^%s' % u'|'.join(user_tps)
    sql_regex = 'REGEXP'
    if connection.vendor == 'postgresql':
        sql_regex = '~'

    if filename:
        query_pootle_path = query_pootle_path + filename
        pootle_path = pootle_path + filename
        resource_path = resource_path + filename

        resources = Store.objects.live().extra(
            where=[
                'pootle_store_store.pootle_path LIKE %s',
                'pootle_store_store.pootle_path ' + sql_regex + ' %s',
            ], params=[query_pootle_path, user_tps_regex]
        ).select_related('translation_project__language')
    else:
        resources = Directory.objects.live().extra(
            where=[
                'pootle_app_directory.pootle_path LIKE %s',
                'pootle_app_directory.pootle_path ' + sql_regex + ' %s',
            ], params=[query_pootle_path, user_tps_regex]
        ).select_related('parent')

    if not resources.exists():
        raise Http404

    request.store = None
    request.directory = None
    request.pootle_path = pootle_path

    request.resource_obj = ProjectResource(resources, pootle_path)
    request.resource_path = resource_path
    request.ctx_obj = path_obj or request.resource_obj
    request.ctx_path = ctx_path


def get_resource(func):
    @wraps(func)
    def wrapped(request, path_obj, dir_path, filename):
        """Gets resources associated to the current context."""
        try:
            directory = getattr(path_obj, 'directory', path_obj)
            if directory.is_project() and (dir_path or filename):
                set_project_resource(request, path_obj, dir_path, filename)
            else:
                set_resource(request, path_obj, dir_path, filename)
        except Http404:
            if not request.is_ajax():
                user_choice = request.COOKIES.get('user-choice', None)
                url = None

                if hasattr(request, 'redirect_url'):
                    url = request.redirect_url
                elif user_choice in ('language', 'resource',):
                    project = (path_obj
                               if isinstance(path_obj, Project)
                               else path_obj.project)
                    url = reverse('pootle-project-browse',
                                  args=[project.code, dir_path, filename])

                if url is not None:
                    response = redirect(url)

                    if user_choice in ('language', 'resource',):
                        # XXX: should we rather delete this in a single place?
                        response.delete_cookie('user-choice')

                    return response

            raise Http404

        return func(request, path_obj, dir_path, filename)

    return wrapped


def permission_required(permission_code):
    """Checks for `permission_code` in the current context.

    To retrieve the proper context, the `get_path_obj` decorator must be
    used along with this decorator.
    """
    def wrapped(func):
        @wraps(func)
        def _wrapped(request, *args, **kwargs):
            path_obj = args[0]
            directory = getattr(path_obj, 'directory', path_obj)

            # HACKISH: some old code relies on
            # `request.translation_project`, `request.language` etc.
            # being set, so we need to set that too.
            attr_name = CLS2ATTR.get(path_obj.__class__.__name__,
                                     'path_obj')
            setattr(request, attr_name, path_obj)

            request.permissions = get_matching_permissions(request.user,
                                                           directory)

            if not permission_code:
                return func(request, *args, **kwargs)

            if not check_permission(permission_code, request):
                raise PermissionDenied(
                    _("Insufficient rights to access this page."),
                )

            return func(request, *args, **kwargs)
        return _wrapped
    return wrapped


def admin_required(func):
    @wraps(func)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_superuser:
            raise PermissionDenied(
                _("You do not have rights to administer Pootle.")
            )
        return func(request, *args, **kwargs)

    return wrapped


class persistent_property(object):
    """
    Similar to cached_property, except it caches in the memory cache rather
    than on the instance if possible.

    By default it will look on the class for an attribute `cache_key` to get
    the class cache_key. The attribute can be changed by setting the `key_attr`
    parameter in the decorator.

    The class cache_key is combined with the name of the decorated property
    to get the final cache_key for the property.

    If no cache_key attribute is present or returns None, it will use instance
    caching by default. This behaviour can be switched off by setting
    `always_cache` to False in the decorator.
    """

    def __init__(self, func, name=None, key_attr=None, always_cache=True):
        self.func = func
        self.__doc__ = getattr(func, '__doc__')
        self.name = name or func.__name__
        self.key_attr = key_attr or "cache_key"
        self.always_cache = always_cache

    def _get_cache_key(self, instance):
        cache_key = getattr(instance, self.key_attr, None)
        if cache_key:
            return "%s/%s" % (cache_key, self.name)

    def __get__(self, instance, cls=None):
        if instance is None:
            return self
        cache_key = self._get_cache_key(instance)
        if cache_key:
            cache = get_cache('lru')
            cached = cache.get(cache_key)
            if cached is not None:
                # cache hit
                return cached
            # cache miss
            res = self.func(instance)
            cache.set(cache_key, res)
            return res
        elif self.always_cache:
            # no cache_key, use instance caching
            res = instance.__dict__[self.name] = self.func(instance)
            return res
        return self.func(instance)
