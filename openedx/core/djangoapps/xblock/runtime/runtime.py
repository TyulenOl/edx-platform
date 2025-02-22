"""
Common base classes for all new XBlock runtimes.
"""
from __future__ import annotations
import logging
from typing import Callable
from urllib.parse import urljoin  # pylint: disable=import-error

import crum
from common.djangoapps.student.models import anonymous_id_for_user
from completion.waffle import ENABLE_COMPLETION_TRACKING_SWITCH
from completion.models import BlockCompletion
from completion.services import CompletionService
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from eventtracking import tracker
from opaque_keys.edx.keys import UsageKey, LearningContextKey
from web_fragments.fragment import Fragment
from xblock.core import XBlock
from xblock.exceptions import NoSuchServiceError
from xblock.field_data import FieldData, SplitFieldData
from xblock.fields import Scope, ScopeIds
from xblock.runtime import KvsFieldData, MemoryIdManager, Runtime

from xmodule.errortracker import make_error_tracker
from xmodule.contentstore.django import contentstore
from xmodule.modulestore.django import XBlockI18nService
from xmodule.services import EventPublishingService, RebindUserService
from xmodule.util.sandboxing import SandboxService
from common.djangoapps.edxmako.services import MakoService
from common.djangoapps.static_replace.services import ReplaceURLService
from common.djangoapps.track import contexts as track_contexts
from common.djangoapps.track import views as track_views
from common.djangoapps.xblock_django.user_service import DjangoXBlockUserService
from lms.djangoapps.courseware.model_data import DjangoKeyValueStore, FieldDataCache
from lms.djangoapps.grades.api import signals as grades_signals
from openedx.core.types import User as UserType
from openedx.core.djangoapps.xblock.apps import get_xblock_app_config
from openedx.core.djangoapps.xblock.data import StudentDataMode
from openedx.core.djangoapps.xblock.runtime.blockstore_field_data import BlockstoreChildrenData, BlockstoreFieldData
from openedx.core.djangoapps.xblock.runtime.ephemeral_field_data import EphemeralKeyValueStore
from openedx.core.djangoapps.xblock.runtime.mixin import LmsBlockMixin
from openedx.core.djangoapps.xblock.utils import get_xblock_id_for_anonymous_user
from openedx.core.lib.cache_utils import CacheService
from openedx.core.lib.xblock_utils import wrap_fragment, xblock_local_resource_url, request_token

from .id_managers import OpaqueKeyReader
from .shims import RuntimeShim, XBlockShim

log = logging.getLogger(__name__)
User = get_user_model()


def make_track_function():
    """
    Make a tracking function that logs what happened, for XBlock events.
    """
    current_request = crum.get_current_request()

    def function(event_type, event):
        return track_views.server_track(current_request, event_type, event, page='x_module')
    return function


class XBlockRuntime(RuntimeShim, Runtime):
    """
    This class manages one or more instantiated XBlocks for a particular user,
    providing those XBlocks with the standard XBlock runtime API (and some
    Open edX-specific additions) so that it can interact with the platform,
    and the platform can interact with it.

    The main reason we cannot make the runtime a long-lived singleton is that
    the XBlock runtime API requires 'user_id' to be a property of the runtime,
    not an argument passed in when loading particular blocks.
    """

    # ** Do not add any XModule compatibility code to this class **
    # Add it to RuntimeShim instead, to help keep legacy code isolated.

    # Feature flags:

    # This runtime can save state for users who aren't logged in:
    suppports_state_for_anonymous_users = True

    # Instance variables:

    user: UserType | None
    user_id: int | str | None
    # dict of FieldData stores for our loaded XBlocks. Key is the block's scope_ids.
    block_field_datas: dict[ScopeIds, FieldData | None]
    # dict of FieldDataCache objects for XBlock with database-based user state
    django_field_data_caches: dict[LearningContextKey, FieldDataCache]

    def __init__(self, system: XBlockRuntimeSystem, user: UserType | None):
        super().__init__(
            id_reader=system.id_reader,
            mixins=(
                LmsBlockMixin,  # Adds Non-deprecated LMS/Studio functionality
                XBlockShim,  # Adds deprecated LMS/Studio functionality / backwards compatibility
            ),
            default_class=None,
            select=None,
            id_generator=system.id_generator,
        )
        self.system = system
        self.user = user
        # self.user_id must be set as a separate attribute since base class sets it:
        if self.user is None:
            self.user_id = None
        elif self.user.is_anonymous:
            self.user_id = get_xblock_id_for_anonymous_user(user)
        else:
            self.user_id = self.user.id
        self.block_field_datas = {}
        self.django_field_data_caches = {}

    def handler_url(self, block, handler_name: str, suffix='', query='', thirdparty=False):
        """
        Get the URL to a specific handler.
        """
        if thirdparty:
            log.warning("thirdparty handlers are not supported by this runtime for XBlock %s.", type(block))

        url = self.system.handler_url(block.scope_ids.usage_id, handler_name, self.user)
        if suffix:
            if not url.endswith('/'):
                url += '/'
            url += suffix
        if query:
            url += '&' if '?' in url else '?'
            url += query
        return url

    def resource_url(self, resource):
        raise NotImplementedError("resource_url is not supported by Open edX.")

    def local_resource_url(self, block: XBlock, uri: str) -> str:
        """
        Get the absolute URL to a resource file (like a CSS/JS file or an image)
        that is part of an XBlock's python module.
        """
        relative_url = xblock_local_resource_url(block, uri)
        site_root_url = get_xblock_app_config().get_site_root_url()
        absolute_url = urljoin(site_root_url, relative_url)
        return absolute_url

    def publish(self, block: XBlock, event_type: str, event_data: dict):
        """ Handle XBlock events like grades and completion """
        special_handler = self.get_event_handler(event_type)
        if special_handler:
            special_handler(block, event_data)
        else:
            self.log_event_to_tracking_log(block, event_type, event_data)

    def get_event_handler(self, event_type: str) -> Callable[[XBlock, dict], None] | None:
        """
        Return an appropriate function to handle the event.

        Returns None if no special processing is required.
        """
        if self.user_id is None:
            # We don't/cannot currently record grades or completion for anonymous users.
            return None
        # In the future when/if we support masquerading, need to be careful here not to affect the user's grades
        if event_type == 'grade':
            return self.handle_grade_event
        elif event_type == 'completion':
            return self.handle_completion_event
        return None

    def log_event_to_tracking_log(self, block: XBlock, event_type: str, event_data: dict) -> None:
        """
        Log this XBlock event to the tracking log
        """
        log_context = track_contexts.context_dict_for_learning_context(block.scope_ids.usage_id.context_key)
        if self.user_id:
            log_context['user_id'] = self.user_id
        log_context['asides'] = {}
        track_function = make_track_function()
        with tracker.get_tracker().context(event_type, log_context):
            track_function(event_type, event_data)

    def handle_grade_event(self, block: XBlock, event: dict):
        """
        Submit a grade for the block.
        """
        if self.user and not self.user.is_anonymous:
            grades_signals.SCORE_PUBLISHED.send(
                sender=None,
                block=block,
                user=self.user,
                raw_earned=event['value'],
                raw_possible=event['max_value'],
                only_if_higher=event.get('only_if_higher'),
                score_deleted=event.get('score_deleted'),
                grader_response=event.get('grader_response')
            )

    def handle_completion_event(self, block: XBlock, event: dict):
        """
        Submit a completion object for the block.
        """
        if not ENABLE_COMPLETION_TRACKING_SWITCH.is_enabled():
            return
        BlockCompletion.objects.submit_completion(
            user=self.user,
            block_key=block.scope_ids.usage_id,
            completion=event['completion'],
        )

    def applicable_aside_types(self, block: XBlock):
        """ Disable XBlock asides in this runtime """
        return []

    def parse_xml_file(self, fileobj, id_generator=None):
        # Deny access to the inherited method
        raise NotImplementedError("XML Serialization is only supported with BlockstoreXBlockRuntime")

    def add_node_as_child(self, block, node, id_generator=None):
        """
        Called by XBlock.parse_xml to treat a child node as a child block.
        """
        # Deny access to the inherited method
        raise NotImplementedError("XML Serialization is only supported with BlockstoreXBlockRuntime")

    def service(self, block: XBlock, service_name: str):
        """
        Return a service, or None.
        Services are objects implementing arbitrary other interfaces.
        """
        # TODO: Do these declarations actually help with anything? Maybe this check should
        # be removed from here and from XBlock.runtime
        declaration = block.service_declaration(service_name)
        context_key = block.scope_ids.usage_id.context_key
        if declaration is None:
            raise NoSuchServiceError(f"Service {service_name!r} was not requested.")
        # Most common service is field-data so check that first:
        if service_name == "field-data":
            if block.scope_ids not in self.block_field_datas:
                try:
                    self.block_field_datas[block.scope_ids] = self._init_field_data_for_block(block)
                except:
                    # Don't try again pointlessly every time another field is accessed
                    self.block_field_datas[block.scope_ids] = None
                    raise
            return self.block_field_datas[block.scope_ids]
        elif service_name == "completion":
            return CompletionService(user=self.user, context_key=context_key)
        elif service_name == "user":
            if self.user is None:
                raise RuntimeError("Cannot access user service when there is no user bound to the XBlock.")
            if self.user.is_anonymous:
                deprecated_anonymous_student_id = self.user_id
            else:
                deprecated_anonymous_student_id = anonymous_id_for_user(self.user, course_id=None)

            return DjangoXBlockUserService(
                self.user,
                # The value should be updated to whether the user is staff in the context when Blockstore runtime adds
                # support for courses.
                user_is_staff=self.user.is_staff,  # type: ignore
                anonymous_user_id=self.anonymous_student_id,
                # See the docstring of `DjangoXBlockUserService`.
                deprecated_anonymous_user_id=deprecated_anonymous_student_id
            )
        elif service_name == "mako":
            if self.system.student_data_mode == StudentDataMode.Ephemeral:
                return MakoService(namespace_prefix='lms.')
            return MakoService()
        elif service_name == "i18n":
            return XBlockI18nService(block=block)
        elif service_name == 'sandbox':
            context_key = block.scope_ids.usage_id.context_key
            return SandboxService(contentstore=contentstore, course_id=context_key)
        elif service_name == 'cache':
            return CacheService(cache)
        elif service_name == 'replace_urls':
            return ReplaceURLService(xblock=block, lookup_asset_url=self._lookup_asset_url)
        elif service_name == 'rebind_user':
            # this service should ideally be initialized with all the arguments of prepare_runtime_for_user
            # but only the positional arguments are passed here as the other arguments are too
            # specific to the lms.block_render module
            return RebindUserService(
                self.user,
                context_key,
                track_function=make_track_function(),
                request_token=request_token(crum.get_current_request()),
            )
        elif service_name == 'publish':
            return EventPublishingService(self.user, context_key, make_track_function())

        # Check if the XBlockRuntimeSystem wants to handle this:
        service = self.system.get_service(block, service_name)
        # Otherwise, fall back to the base implementation which loads services
        # defined in the constructor:
        if service is None:
            service = super().service(block, service_name)
        return service

    def _init_field_data_for_block(self, block: XBlock) -> FieldData:
        """
        Initialize the FieldData implementation for the specified XBlock
        """
        if self.user is None:
            # No user is specified, so we want to throw an error if anything attempts to read/write user-specific fields
            student_data_store = None
        elif self.user.is_anonymous:
            # This is an anonymous (non-registered) user:
            assert isinstance(self.user_id, str) and self.user_id.startswith("anon")
            kvs = EphemeralKeyValueStore()
            student_data_store = KvsFieldData(kvs)
        elif self.system.student_data_mode == StudentDataMode.Ephemeral:
            # We're in an environment like Studio where we want to let the
            # author test blocks out but not permanently save their state.
            kvs = EphemeralKeyValueStore()
            student_data_store = KvsFieldData(kvs)
        else:
            # Use database-backed field data (i.e. store user_state in StudentModule)
            context_key = block.scope_ids.usage_id.context_key
            if context_key not in self.django_field_data_caches:
                field_data_cache = FieldDataCache(
                    [block], course_id=context_key, user=self.user, asides=None, read_only=False,
                )
                self.django_field_data_caches[context_key] = field_data_cache
            else:
                field_data_cache = self.django_field_data_caches[context_key]
                field_data_cache.add_blocks_to_cache([block])
            student_data_store = KvsFieldData(kvs=DjangoKeyValueStore(field_data_cache))

        return SplitFieldData({
            Scope.content: self.system.authored_data_store,
            Scope.settings: self.system.authored_data_store,
            Scope.parent: self.system.authored_data_store,
            Scope.children: self.system.children_data_store,
            Scope.user_state_summary: student_data_store,
            Scope.user_state: student_data_store,
            Scope.user_info: student_data_store,
            Scope.preferences: student_data_store,
        })

    def render(self, block: XBlock, view_name: str, context: dict | None = None):
        """
        Render a specific view of an XBlock.
        """
        # Users who aren't logged in are not allowed to view any views other
        # than public_view. They may call any handlers though.
        if (self.user is None or self.user.is_anonymous) and view_name != 'public_view':
            raise PermissionDenied

        # We also need to override this method because some XBlocks in the
        # edx-platform codebase use methods from builtin_assets.py,
        # which create relative URLs (/static/studio/bundles/webpack-foo.js).
        # We want all resource URLs to be absolute, such as is done when
        # local_resource_url() is used.
        fragment = super().render(block, view_name, context)
        needs_fix = False
        for resource in fragment.resources:
            if resource.kind == 'url' and resource.data.startswith('/'):
                needs_fix = True
                break
        if needs_fix:
            log.warning("XBlock %s returned relative resource URLs, which are deprecated", block.scope_ids.usage_id)
            # The Fragment API is mostly immutable, so changing a resource requires this:
            frag_data = fragment.to_dict()
            for resource in frag_data['resources']:
                if resource['kind'] == 'url' and resource['data'].startswith('/'):
                    log.debug("-> Relative resource URL: %s", resource['data'])
                    resource['data'] = get_xblock_app_config().get_site_root_url() + resource['data']
            fragment = Fragment.from_dict(frag_data)

        # Apply any required transforms to the fragment.
        # We could move to doing this in wrap_xblock() and/or use an array of
        # wrapper methods like the ConfigurableFragmentWrapper mixin does.
        fragment = wrap_fragment(
            fragment,
            ReplaceURLService(xblock=block, lookup_asset_url=self._lookup_asset_url).replace_urls(fragment.content)
        )

        return fragment

    def _lookup_asset_url(self, block: XBlock, asset_path: str):  # pylint: disable=unused-argument
        """
        Return an absolute URL for the specified static asset file that may
        belong to this XBlock.

        e.g. if the XBlock settings have a field value like "/static/foo.png"
        then this method will be called with asset_path="foo.png" and should
        return a URL like https://cdn.none/xblock/f843u89789/static/foo.png

        If the asset file is not recognized, return None
        """
        # Subclasses should override this
        return None


class XBlockRuntimeSystem:
    """
    This class is essentially a factory for XBlockRuntimes. This is a
    long-lived object which provides the behavior specific to the application
    that wants to use XBlocks. Unlike XBlockRuntime, a single instance of this
    class can be used with many different XBlocks, whereas each XBlock gets its
    own instance of XBlockRuntime.
    """
    def __init__(
        self,
        handler_url: Callable[[UsageKey, str, UserType | None], str],
        student_data_mode: StudentDataMode,
        runtime_class: type[XBlockRuntime],
    ):
        """
        args:
            handler_url: A method to get URLs to call XBlock handlers. It must
                implement this signature:
                handler_url(
                    usage_key: UsageKey,
                    handler_name: str,
                    user: User | AnonymousUser | None
                ) -> str
            student_data_mode: Specifies whether student data should be kept
                in a temporary in-memory store (e.g. Studio) or persisted
                forever in the database.
            runtime_class: What runtime to use, e.g. BlockstoreXBlockRuntime
        """
        self.handler_url = handler_url
        self.id_reader = OpaqueKeyReader()
        self.id_generator = MemoryIdManager()  # We don't really use id_generator until we need to support asides
        self.runtime_class = runtime_class
        self.authored_data_store = BlockstoreFieldData()
        self.children_data_store = BlockstoreChildrenData(self.authored_data_store)
        assert student_data_mode in (StudentDataMode.Ephemeral, StudentDataMode.Persisted)
        self.student_data_mode = student_data_mode

    def get_runtime(self, user: UserType | None) -> XBlockRuntime:
        """
        Get the XBlock runtime for the specified Django user. The user can be
        a regular user, an AnonymousUser, or None.
        """
        return self.runtime_class(self, user)

    def get_service(self, block, service_name: str):
        """
        Get a runtime service

        Runtime services may come from this XBlockRuntimeSystem,
        or if this method returns None, they may come from the
        XBlockRuntime.
        """
        if service_name == 'error_tracker':
            return make_error_tracker()
        return None  # None means see if XBlockRuntime offers this service
