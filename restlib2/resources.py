import hashlib
import mimeparse
# http://mail.python.org/pipermail/python-list/2010-March/1239510.html
from calendar import timegm
from datetime import datetime, timedelta
from django.conf import settings
from django.http import HttpResponse, HttpRequest
from django.core.cache import cache
from django.utils.http import http_date, parse_http_date
from http import codes, methods
from serializers import serializers

EPOCH_DATE = datetime(1970, 1, 1, 0, 0, 0)

# Convenience function for checking for existent, callable methods
usable = lambda x, y: callable(getattr(x, y, None))

# ## Resource Metaclass
# Sets up a few helper components for the `Resource` class.
class ResourceMetaclass(type):

    def __new__(cls, name, bases, attrs):
        # Create the new class as is to start. Subclass attributes can be
        # checked for in `attrs` and handled as necessary relative to the base
        # classes.
        new_cls = type.__new__(cls, name, bases, attrs)

        # If `allowed_methods` is not defined explicitly in attrs, this
        # could mean one of two things: that the user wants it to inherit
        # from the parent class (if exists) or for it to be set implicitly.
        # The more explicit (and flexible) behavior will be to not inherit
        # it from the parent class, therefore the user must explicitly
        # re-set the attribute.
        if 'allowed_methods' not in attrs or not new_cls.allowed_methods:
            allowed_methods = []

            for method in methods:
                if usable(new_cls, method.lower()):
                    allowed_methods.append(method)

        # If the attribute is defined in this subclass, ensure all methods that
        # are said to be allowed are actually defined and callable.
        else:
            allowed_methods = list(new_cls.allowed_methods)

            for method in allowed_methods:
                if not usable(new_cls, method.lower()):
                    raise ValueError('The %s method is not defined for the '
                        'resource %s' % (method, new_cls.__name__))

        # If `GET` is not allowed, remove `HEAD` method.
        if 'GET' not in allowed_methods and 'HEAD' in allowed_methods:
            allowed_methods.remove('HEAD')

        new_cls.allowed_methods = tuple(allowed_methods)

        if not new_cls.supported_content_types:
            new_cls.supported_content_types = new_cls.supported_accept_types

        if not new_cls.supported_patch_types:
            new_cls.supported_patch_types = new_cls.supported_content_types

        return new_cls

    def __call__(cls, *args, **kwargs):
        """Tests to see if the first argument is an HttpRequest object, creates
        an instance, and calls it with the arguments.
        """
        if args and isinstance(args[0], HttpRequest):
            instance = super(ResourceMetaclass, cls).__call__()
            return instance.__call__(*args, **kwargs)
        return super(ResourceMetaclass, cls).__call__(*args, **kwargs)


# ## Resource
# Comprehensive ``Resource`` class which implements sensible request
# processing. The process flow is largely derived from Alan Dean's
# [status code activity diagram][0].
#
# ### Implementation Considerations
# [Section 2][1] of the HTTP/1.1 specification states:
#
# > The methods GET and HEAD MUST be supported by all general-purpose servers.
# > All other methods are OPTIONAL;
#
# The `HEAD` handler is already implemented on the `Resource` class, but
# requires the `GET` handler to be implemented. Although not required, the
# `OPTIONS` handler is also implemented.
#
# Response representations should follow the rules outlined in [Section 5.1][2].
#
# [Section 6.1][3] defines that `GET`, `HEAD`, `OPTIONS` and `TRACE` are
# considered _safe_ methods, thus ensure the implementation of these methods do
# not have any side effects. In addition to the safe methods, `PUT` and
# `DELETE` are considered _idempotent_ which means subsequent identical requests
# to the same resource does not result it different responses to the client.
#
# Request bodies on `GET`, `HEAD`, `OPTIONS`, and `DELETE` requests are
# ignored. The HTTP spec does not define any semantics surrounding this
# situtation.
#
# Typical uses of `POST` requests are described in [Section 6.5][4], but in most
# cases should be assumed by clients as _black box_, neither safe nor idempotent.
# If updating an existing resource, it is more appropriate to use `PUT`.
#
# [Section 7.2.1][5] defines that `GET`, `HEAD`, `POST`, and 'TRACE' should have
# a payload for status code of 200 OK. If not supplied, a different 2xx code may
# be more appropriate.
#
# [0]: http://code.google.com/p/http-headers-status/downloads/detail?name=http-headers-status%20v3%20draft.png
# [1]: http://tools.ietf.org/html/draft-ietf-httpbis-p2-semantics-18#section-2
# [2]: http://tools.ietf.org/html/draft-ietf-httpbis-p2-semantics-18#section-5.1
# [3]: http://tools.ietf.org/html/draft-ietf-httpbis-p2-semantics-18#section-6.1
# [4]: http://tools.ietf.org/html/draft-ietf-httpbis-p2-semantics-18#section-6.5
class Resource(object):

    __metaclass__ = ResourceMetaclass

    # ### Service Availability
    # Toggle this resource as unavailable. If `True`, the service
    # will be unavailable indefinitely. If an integer or datetime is
    # used, the `Retry-After` header will set. An integer can be used
    # to define a seconds delta from the current time (good for unexpected
    # downtimes). If a datetime is set, the number of seconds will be
    # calculated relative to the current time (good for planned downtime).
    unavailable = False

    # ### Allowed Methods
    # If `None`, the allowed methods will be determined based on the resource
    # methods define, e.g. `get`, `put`, `post`. A list of methods can be
    # defined explicitly to have not expose defined methods.
    allowed_methods = None

    # ### Request Rate Limiting
    # Enforce request rate limiting. Both `rate_limit_count` and
    # `rate_limit_seconds` must be defined and not zero to be active.
    # By default, the number of seconds defaults to 1 hour, but the count
    # is `None`, therefore rate limiting is not enforced.
    rate_limit_count = None
    rate_limit_seconds = 60 * 60

    # ### Max Request Entity Length
    # If not `None`, checks if the request entity body is too large to
    # be processed.
    max_request_entity_length = None

    # ### Supported _Accept_ Mimetypes
    # Define a list of mimetypes supported for encoding response entity
    # bodies. Default to `('application/json',)`
    # _See also: `supported_content_types`_
    supported_accept_types = ('application/json',)

    # ### Supported _Content-Type_ Mimetypes
    # Define a list of mimetypes supported for decoding request entity bodies.
    # This is independent of the mimetypes encoders for request bodies.
    # Defaults to mimetypes defined in `supported_accept_types`.
    supported_content_types = None

    # ### Supported PATCH Mimetypes
    # Define a list of mimetypes supported for decoding request entity bodies
    # for `PATCH` requests. Defaults to mimetypes defined in
    # `supported_content_types`.
    supported_patch_types = None

    # ### Validation Caching

    # #### Require Conditional Request
    # If `True`, `PUT` and `PATCH` requests are required to have a conditional
    # header for verifying the operation applies to the current state of the
    # resource on the server. This must be used in conjunction with either
    # the `use_etags` or `use_last_modified` option to take effect.
    require_conditional_request = False

    # #### Use ETags
    # If `True`, the `ETag` header will be set on responses and conditional
    # requests are supported. This applies to _GET_, _HEAD_, _PUT_, _PATCH_
    # and _DELETE_ requests. Defaults to Django's `USE_ETAGS` setting.
    use_etags = settings.USE_ETAGS

    # #### Use Last Modified
    # If `True`, the `Last-Modified` header will be set on responses and
    # conditional requests are supported. This applies to _GET_, _HEAD_, _PUT_,
    # _PATCH_ and _DELETE_ requests.
    use_last_modified = False

    # ### Expiration Caching

    # Define a default `Cache-Control` setting for cacheable responses.
    # Setting `local_cache_age` to some value in seconds enables local
    # cache of the response, e.g. a browser. Setting `proxy_cache_age`
    # allows proxies to also cache the response. Do **not** enable cache
    # for proxies if this is user-specific content.
    # References:
    # - http://www.odino.org/301/rest-better-http-cache
    # - http://www.subbu.org/blog/2005/01/http-caching
    local_cache_age = None
    proxy_cache_age = None

    # #### Availability over consistency...
    # Allow for stale cache
    stale_cache_on_error = None
    stale_cache_on_revalidate = None

    # ## Initialize Once, Process Many
    # Every `Resource` class can be initialized once since they are stateless
    # (and thus thread-safe).
    def __call__(self, request, *args, **kwargs):
        # Process the request. This includes all the necessary checks prior to
        # actually interfacing with the resource itself.
        response = self.process_request(request, *args, **kwargs)

        if not isinstance(response, HttpResponse):
            # Attempt to process the request given the corresponding `request.method`
            # handler.
            method_handler = getattr(self, request.method.lower())
            try:
                response = method_handler(request, *args, **kwargs)
            except Exception, exception:
                response = self.process_exception(request, exception)
                if isinstance(response, HttpResponse):
                    return response
                raise exception

        # Process the response, check if the response is overridden and
        # use that instead.
        # TODO not sure if this is sound for a simple resource
        return self.process_response(request, response)

    def process_exception(self, request, exception):
        "Override to handle any exception raised during processing."
        # Do nothing. Let Django process the exception
        raise exception

    def process_request(self, request, *args, **kwargs):
        # Initilize a new response for this request. Passing the response along
        # the request cycle allows for gradual modification of the headers.
        response = HttpResponse()

        # TODO keep track of a list of request headers used to
        # determine the resource representation for the 'Vary'
        # header.

        # ### 503 Service Unavailable
        # The server does not need to be unavailable for a resource to be
        # unavailable...
        if self.is_service_unavailable(request, response, *args, **kwargs):
            response.status_code = codes.service_unavailable
            return response

        # ### 414 Request URI Too Long _(not implemented)_
        # This should be be handled upstream by the Web server

        # ### 400 Bad Request _(not implemented)_
        # Note that many services respond with this code when entities are
        # unprocessable. This should really be a 422 Unprocessable Entity

        # ### 401 Unauthorized
        # Check if the request is authorized to access this resource.
        if self.is_unauthorized(request, response, *args, **kwargs):
            response.status_code = codes.unauthorized
            return response

        # ### 403 Forbidden
        # Check if this resource is forbidden for the request.
        if self.is_forbidden(request, response, *args, **kwargs):
            response.status_code = codes.forbidden
            return response

        # ### 501 Not Implemented _(not implemented)_
        # This technically refers to a service-wide response for an
        # unimplemented request method.

        # ### 429 Too Many Requests
        # Both `rate_limit_count` and `rate_limit_seconds` must be none
        # falsy values to be checked.
        if self.rate_limit_count and self.rate_limit_seconds:
            if self.is_too_many_requests(request, response, *args, **kwargs):
                response.status_code = codes.too_many_requests
                return response

        # ### Process an _OPTIONS_ request
        # Enough processing has been performed to allow an OPTIONS request.
        if request.method == methods.OPTIONS and 'OPTIONS' in self.allowed_methods:
            return self.options(request, response)

        # ## Request Entity Checks
        # Only perform these checks if the request has supplied a body.
        if 'CONTENT_LENGTH' in request.META and request.META['CONTENT_LENGTH']:

            # ### 415 Unsupported Media Type
            # Check if the entity `Content-Type` supported for decoding.
            if self.is_unsupported_media_type(request, response, *args, **kwargs):
                response.status_code = codes.unsupported_media_type
                return response

            # ### 413 Request Entity Too Large
            # Check if the entity is too large for processing
            if self.max_request_entity_length:
                if self.is_request_entity_too_large(request, response, *args, **kwargs):
                    response.status_code = codes.request_entity_too_large
                    return response

        # ### 405 Method Not Allowed
        if self.is_method_not_allowed(request, response, *args, **kwargs):
            response.status_code = codes.method_not_allowed
            return response

        # ### 406 Not Acceptable
        # Checks Accept and Accept-* headers
        if self.is_not_acceptable(request, response, *args, **kwargs):
            response.status_code = codes.not_acceptable
            return response

        # ### 404 Not Found
        # Check if this resource exists.
        if self.is_not_found(request, response, *args, **kwargs):
            response.status_code = codes.not_found
            return response

        # ### 410 Gone
        # Check if this resource used to exist, but does not anymore.
        if self.is_gone(request, response, *args, **kwargs):
            response.status_code = codes.gone
            return response

        # ### 428 Precondition Required
        # Prevents the "lost udpate" problem and requires client to confirm
        # the state of the resource has not changed since the last `GET`
        # request. This applies to `PUT` and `PATCH` requests.
        if self.require_conditional_request:
            if request.method == methods.PUT or request.method == methods.PATCH:
                if self.is_precondition_required(request, response, *args, **kwargs):
                    # HTTP/1.1
                    response['Cache-Control'] = 'no-cache'
                    # HTTP/1.0
                    response['Pragma'] = 'no-cache'
                    response.status_code = codes.precondition_required
                    return response

        # ### 412 Precondition Failed
        # Conditional requests applies to GET, HEAD, PUT, and PATCH.
        # For GET and HEAD, the request checks the either the entity changed
        # since the last time it requested it, `If-Modified-Since`, or if the
        # entity tag (ETag) has changed, `If-None-Match`.
        if request.method == methods.PUT or request.method == methods.PATCH:
            if self.is_precondition_failed(request, response, *args, **kwargs):
                # HTTP/1.1
                response['Cache-Control'] = 'no-cache'
                # HTTP/1.0
                response['Pragma'] = 'no-cache'
                response.status_code = codes.precondition_failed
                return response

        # Check for conditional GET or HEAD request
        if request.method == methods.GET or request.method == methods.HEAD:
            if self.use_etags and 'HTTP_IF_NONE_MATCH' in request.META:
                request_etag = request.META['HTTP_IF_NONE_MATCH'].strip('"')
                etag = self.get_etag(request, request_etag)
                if request_etag == etag:
                    response.status_code = codes.not_modified
                    return response

            if self.use_last_modified and 'HTTP_IF_MODIFIED_SINCE' in request.META:
                last_modified = self.get_last_modified(request, *args, **kwargs)
                known_last_modified = EPOCH_DATE + timedelta(seconds=parse_http_date(request.META['HTTP_IF_MODIFIED_SINCE']))
                if known_last_modified >= last_modified:
                    response.status_code = codes.not_modified
                    return response

    # ## Process the normal response returned by the handler
    def process_response(self, request, response):
        content = ''
        content_length = 0

        if isinstance(response, HttpResponse):
            if hasattr(response, '_raw_content'):
                content = response._raw_content
                del response._raw_content
        else:
            content = response
            response = HttpResponse()

        # If the response already has a `_raw_content` attribute, do not
        # bother with the local content.
        if isinstance(content, basestring):
            response.content = content
        else:
            if hasattr(request, '_accept_type'):
                # Encode the body
                content = serializers.encode(request._accept_type, content)
                response['Content-Type'] = request._accept_type

        if content is not None:
            response.content = content
            content_length = len(response.content)

        if content_length == 0:
            del response['Content-Type']
            if response.status_code == codes.ok:
                response.status_code = codes.no_content
        else:
            response['Content-Length'] = str(content_length)

        if self.use_etags:
            self.set_etag(request, response)

        return response

    # ## Request Programatically
    # For composite resources, `resource.apply` can be used on related resources
    # with the original `request`.
    def apply(self, request, *args, **kargs):
        pass

    # ## Request Method Handlers
    # ### _HEAD_ Request Handler
    # Default handler for _HEAD_ requests. For this to be available,
    # a _GET_ handler must be defined.
    def head(self, request, *args, **kwargs):
        self.get(request, *args, **kwargs)

    # ### _OPTIONS_ Request Handler
    # Default handler _OPTIONS_ requests.
    def options(self, request, *args, **kwargs):
        response = HttpResponse()

        # See [RFC 5789][0]
        # [0]: http://tools.ietf.org/html/rfc5789#section-3.1
        if 'PATCH' in self.allowed_methods:
            response['Accept-Patch'] = ', '.join(self.supported_patch_types)

        response['Allow'] = ', '.join(sorted(self.allowed_methods))
        response['Content-Length'] = 0
        # HTTP/1.1
        response['Cache-Control'] = 'no-cache'
        # HTTP/1.0
        response['Pragma'] = 'no-cache'
        return response


    # ## Response Status Code Handlers
    # Each handler prefixed with `is_` corresponds to various client (4xx)
    # and server (5xx) error checking. For example, `is_not_found` will
    # return `True` if the resource does not exit. _Note: all handlers are
    # must return `True` to fail the check._

    # ### Service Unavailable
    # Checks if the service is unavailable based on the `unavailable` flag.
    # Set the `Retry-After` header if possible to inform clients when
    # the resource is expected to be available.
    # See also: `unavailable`
    def is_service_unavailable(self, request, response, *args, **kwargs):
        if self.unavailable:
            if type(self.unavailable) is int and self.unavailable > 0:
                retry = self.unavailable
            elif type(self.unavailable) is datetime:
                retry = http_date(timegm(self.unavailable.utctimetuple()))
            else:
                retry = None

            if retry:
                response['Retry-After'] = retry
            return True
        return False

    # ### Unauthorized
    # Checks if the request is authorized to access this resource.
    # Default is a no-op.
    def is_unauthorized(self, request, response, *args, **kwargs):
        return False

    # ### Forbidden
    # Checks if the request is forbidden. Default is a no-op.
    def is_forbidden(self, request, response, *args, **kwargs):
        return False

    # ### Too Many Requests
    # Checks if this request is rate limited. Default is a no-op.
    def is_too_many_requests(self, request, response, *args, **kwargs):
        return False

    # ### Request Entity Too Large
    # Check if the request entity is too large to process.
    def is_request_entity_too_large(self, request, response, *args, **kwargs):
        if request.META['CONTENT_LENGTH'] > self.max_request_entity_length:
            return True

    # ### Method Not Allowed
    # Check if the request method is not allowed.
    def is_method_not_allowed(self, request, response, *args, **kwargs):
        if request.method not in self.allowed_methods:
            response['Allow'] = ', '.join(sorted(self.allowed_methods))
            return True
        return False

    # ### Unsupported Media Type
    # Check if this resource can process the request entity body. Note
    # `Content-Type` is set as the empty string, so ensure it is not falsy
    # when processing it.
    def is_unsupported_media_type(self, request, response, *args, **kwargs):
        if 'CONTENT_TYPE' in request.META:
            if not self.content_type_supported(request, response):
                return True

            if not self.content_encoding_supported(request, response):
                return True

            if not self.content_language_supported(request, response):
                return True

        return False

    # ### Not Acceptable
    # Check if this resource can return an acceptable response.
    def is_not_acceptable(self, request, response, *args, **kwargs):
        if not self.accept_type_supported(request, response):
            return True

        if 'HTTP_ACCEPT_LANGUAGE' in request.META:
            if not self.accept_language_supported(request, response):
                return True

        if 'HTTP_ACCEPT_CHARSET' in request.META:
            if not self.accept_charset_supported(request, response):
                return True

        if 'HTTP_ACCEPT_ENCODING' in request.META:
            if not self.accept_encoding_supported(request, response):
                return True

        return False

    # ### Precondition Required
    # Check if a conditional request is
    def is_precondition_required(self, request, response, *args, **kwargs):
        if self.use_etags and 'HTTP_IF_MATCH' not in request.META:
            return True
        if self.use_last_modified and 'HTTP_IF_UNMODIFIED_SINCE' not in request.META:
            return True
        return False

    def is_precondition_failed(self, request, response, *args, **kwargs):
        # ETags are enabled. Check for conditional request headers. The current
        # ETag value is used for the conditional requests. After the request
        # method handler has been processed, the new ETag will be calculated.
        if self.use_etags and 'HTTP_IF_MATCH' in request.META:
            request_etag = request.META['HTTP_IF_MATCH'].strip('"')
            etag = self.get_etag(request, request_etag)
            if request_etag != etag:
                return True

        # Last-Modified date enabled. check for conditional request headers. The
        # current modification datetime value is used for the conditional
        # requests. After the request method handler has been processed, the new
        # Last-Modified datetime will be returned.
        if self.use_last_modified and 'HTTP_IF_UNMODIFIED_SINCE' in request.META:
            last_modified = self.get_last_modified(request, *args, **kwargs)
            known_last_modified = EPOCH_DATE + timedelta(seconds=parse_http_date(request.META['HTTP_IF_UNMODIFIED_SINCE']))
            if  known_last_modified != last_modified:
                return True

        return False


    # ### Not Found
    # Checks if the requested resource exists.
    def is_not_found(self, request, response, *args, **kwargs):
        return False

    # ### Gone
    # Checks if the resource _no longer_ exists.
    def is_gone(self, request, response, *args, **kwargs):
        return False



    # ## Request Accept-* handlers

    # Checks if the requested `Accept` mimetype is supported. Defaults
    # to using the first specified mimetype in `supported_accept_types`.
    def accept_type_supported(self, request, response):
        if 'HTTP_ACCEPT' in request.META:
            accept_type = request.META['HTTP_ACCEPT']
            mimetypes = list(self.supported_accept_types)
            mimetypes.reverse()
            match = mimeparse.best_match(mimetypes, accept_type)

            if match:
                request._accept_type = match
                return True

            # Only if `Accept` explicitly contains a `*/*;q=0.0`
            # does it preclude from returning a non-matching mimetype.
            # This may be desirable behavior (or not), so add this as an
            # option, e.g. `force_accept_type`
            if mimeparse.quality('*/*', accept_type) == 0:
                return False

        # If `supported_accept_types` is empty, it is assumed that the resource
        # will return whatever it wants.
        if len(self.supported_accept_types):
            response._accept_type = self.supported_accept_types[0]
        return True

    # Checks if the requested `Accept-Charset` is supported.
    def accept_charset_supported(self, request, response):
        return True

    # Checks if the requested `Accept-Encoding` is supported.
    def accept_encoding_supported(self, request, response):
        return True

    # Checks if the requested `Accept-Language` is supported.
    def accept_language_supported(self, request, response):
        return True


    # ## Conditionl Request Handlers

    # ### Get/Calculate ETag
    # Calculates an etag for the requested entity.
    # Provides the client an entity tag for future conditional
    # requests.
    # For GET and HEAD requests the `If-None-Match` header may be
    # set to check if the entity has changed since the last request.
    # For PUT, PATCH, and DELETE requests, the `If-Match` header may be
    # set to ensure the entity is the same as the cllient's so the current
    # operation is valid (optimistic concurrency).
    def get_etag(self, request, etag=None):
        # Check cache first
        if etag is not None and etag in cache:
            return etag

    def set_etag(self, request, response):
        if 'ETag' in response:
            etag = response['ETag'].strip('"')
        else:
            etag = hashlib.md5(response.content).hexdigest()
            response['ETag'] = '"{}"'.format(etag)
        cache.set(etag, 1, 20)

    # ### Calculate Last Modified Datetime
    # Calculates the last modified time for the requested entity.
    # Provides the client the last modified of the entity for future
    # conditional requests.
    def get_last_modified(self, request):
        return datetime.now()

    # ### Calculate Expiry Datetime
    # (not implemented)
    # Gets the expiry date and time for the requested entity.
    # Informs the client when the entity will be invalid. This is most
    # useful for clients to only refresh when they need to, otherwise the
    # client's local cache is used.
    def get_expiry(self, request, *args, **kwargs):
        pass


    # ## Entity Content-* handlers

    # Check if the request Content-Type is supported by this resource
    # for decoding.
    def content_type_supported(self, request, response, *args, **kwargs):
        content_type = request.META['CONTENT_TYPE']
        mimetypes = list(self.supported_content_types)
        mimetypes.reverse()
        match = mimeparse.best_match(mimetypes, content_type)
        if match:
            request._content_type = match
            return True
        return False

    def content_encoding_supported(self, request, response, *args, **kwargs):
        return True

    def content_language_supported(self, request, response, *args, **kwargs):
        return True