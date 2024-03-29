# -*- coding: utf-8 -*-
#########################################################################
#
# Copyright (C) 2016 OSGeo
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
#########################################################################

import base64
import copy
import datetime
import logging
import math
import os
import re
import uuid
import subprocess
import select
from StringIO import StringIO

from osgeo import ogr
from slugify import Slugify
import string

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
# use lazy gettext because some translated strings are used before
# i18n infra is up
from django.utils.translation import ugettext_lazy as _
from django.db import models, connection, transaction
from django.core.serializers.json import DjangoJSONEncoder
import httplib2
import urlparse
import urllib

import gc
import weakref

try:
    import json
except ImportError:
    from django.utils import simplejson as json

import sys
reload(sys)
sys.setdefaultencoding('utf8')

DEFAULT_URL = ""
DEFAULT_TITLE = ""
DEFAULT_ABSTRACT = ""
DEFAULT_CONTENT=(
      '<h3>关于我们</h3>\
  <p>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;\
  灿烂辉煌的人类文明、浩如烟海的古今文献以及广袤无垠的\
  陆地海洋，存在着海量的与人类活动息息相关的地理信息。\
  就单个人物来说，包括人物的籍贯、行迹、社会关系的地理分布；\
  就群体来说，包括一个群体的地理分布和迁徙轨迹；就非生命\
  的物体来说，也有其存在、分布和变化的地理区域；就一个地\
  方来说，则又包含了既往时间里人、事、物等地理信息的总汇。</p>\
  <p>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;\
  由浙江大学与哈佛大学共建的学术地图发布平台愿为广大\
  用户提供地理信息研究成果的发布、可视化分析及多功能查\
  询服务，平台所形成的大数据，可以为未来科学研究、政府决\
  策及社会服务提供重要的参考。</p>\
  <p style="text-align: right">浙江大学大数据与中国学术地图创新团队<br>\
  哈佛大学地理分析中心<br>\
  2017年12月18日<br></p>\
  <h3>版权声明</h3>\
  <p>　&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;\
  “学术地图发布平台”的版权归“浙江大学大数据与中国学术\
  地图创新团队”和“哈佛大学地理分析中心”共同所有。学者上传的\
  地理信息数据与呈现的地图，版权归发布者和平台方共同所有。</p>'
)

INVALID_PERMISSION_MESSAGE = _("Invalid permission level.")

ALPHABET = string.ascii_uppercase + string.ascii_lowercase + \
    string.digits + '-_'
ALPHABET_REVERSE = dict((c, i) for (i, c) in enumerate(ALPHABET))
BASE = len(ALPHABET)
SIGN_CHARACTER = '$'
SQL_PARAMS_RE = re.compile(r'%\(([\w_\-]+)\)s')

http_client = httplib2.Http()
_user = settings.OGC_SERVER_DEFAULT_USER
_password = settings.OGC_SERVER_DEFAULT_PASSWORD
http_client.add_credentials(_user, _password)
http_client.add_credentials(_user, _password)
_netloc = urlparse.urlparse(settings.GEOSERVER_LOCATION).netloc
http_client.authorizations.append(
    httplib2.BasicAuthentication(
        (_user, _password),
        _netloc,
        settings.GEOSERVER_LOCATION,
        {},
        None,
        None,
        http_client
    )
)
custom_slugify = Slugify(separator='_')

signalnames = [
    'class_prepared',
    'm2m_changed',
    'post_delete',
    'post_init',
    'post_save',
    'post_syncdb',
    'pre_delete',
    'pre_init',
    'pre_save']
signals_store = {}

id_none = id(None)

logger = logging.getLogger("geonode.utils")


def _get_basic_auth_info(request):
    """
    grab basic auth info
    """
    meth, auth = request.META['HTTP_AUTHORIZATION'].split()
    if meth.lower() != 'basic':
        raise ValueError
    username, password = base64.b64decode(auth).split(':')
    return username, password


def batch_permissions(request):
    # TODO
    pass


def batch_delete(request):
    # TODO
    pass


def _split_query(query):
    """
    split and strip keywords, preserve space
    separated quoted blocks.
    """

    qq = query.split(' ')
    keywords = []
    accum = None
    for kw in qq:
        if accum is None:
            if kw.startswith('"'):
                accum = kw[1:]
            elif kw:
                keywords.append(kw)
        else:
            accum += ' ' + kw
            if kw.endswith('"'):
                keywords.append(accum[0:-1])
                accum = None
    if accum is not None:
        keywords.append(accum)
    return [kw.strip() for kw in keywords if kw.strip()]


def bbox_to_wkt(x0, x1, y0, y1, srid="4326"):
    if srid and srid.startswith('EPSG:'):
        srid = srid[5:]
    if None not in [x0, x1, y0, y1]:
        wkt = 'SRID=%s;POLYGON((%s %s,%s %s,%s %s,%s %s,%s %s))' % (
            srid, x0, y0, x0, y1, x1, y1, x1, y0, x0, y0)
    else:
        wkt = 'SRID=4326;POLYGON((-180 -90,-180 90,180 90,180 -90,-180 -90))'
    return wkt


def llbbox_to_mercator(llbbox):
    minlonlat = forward_mercator([llbbox[0], llbbox[2]])
    maxlonlat = forward_mercator([llbbox[1], llbbox[3]])
    return [minlonlat[0], minlonlat[1], maxlonlat[0], maxlonlat[1]]


def mercator_to_llbbox(bbox):
    minlonlat = inverse_mercator([bbox[0], bbox[2]])
    maxlonlat = inverse_mercator([bbox[1], bbox[3]])
    return [minlonlat[0], minlonlat[1], maxlonlat[0], maxlonlat[1]]


def forward_mercator(lonlat):
    """
        Given geographic coordinates, return a x,y tuple in spherical mercator.

        If the lat value is out of range, -inf will be returned as the y value
    """
    x = lonlat[0] * 20037508.34 / 180
    try:
        # With data sets that only have one point the value of this
        # expression becomes negative infinity. In order to continue,
        # we wrap this in a try catch block.
        n = math.tan((90 + lonlat[1]) * math.pi / 360)
    except ValueError:
        n = 0
    if n <= 0:
        y = float("-inf")
    else:
        y = math.log(n) / math.pi * 20037508.34
    return (x, y)


def inverse_mercator(xy):
    """
        Given coordinates in spherical mercator, return a lon,lat tuple.
    """
    lon = (xy[0] / 20037508.34) * 180
    lat = (xy[1] / 20037508.34) * 180
    lat = 180 / math.pi * \
        (2 * math.atan(math.exp(lat * math.pi / 180)) - math.pi / 2)
    return (lon, lat)


def layer_from_viewer_config(model, layer, source, ordering):
    """
    Parse an object out of a parsed layer configuration from a GXP
    viewer.

    ``model`` is the type to instantiate
    ``layer`` is the parsed dict for the layer
    ``source`` is the parsed dict for the layer's source
    ``ordering`` is the index of the layer within the map's layer list
    """
    layer_cfg = dict(layer)
    for k in ["format", "name", "opacity", "styles", "transparent",
              "fixed", "group", "visibility", "source", "getFeatureInfo"]:
        if k in layer_cfg:
            del layer_cfg[k]
    layer_cfg["wrapDateLine"] = True
    layer_cfg["displayOutsideMaxExtent"] = True

    source_cfg = dict(source)
    for k in ["url", "projection"]:
        if k in source_cfg:
            del source_cfg[k]

    # We don't want to hardcode 'access_token' into the storage
    if 'capability' in layer_cfg:
        capability = layer_cfg['capability']
        if 'styles' in capability:
            styles = capability['styles']
            for style in styles:
                if 'legend' in style:
                    legend = style['legend']
                    if 'href' in legend:
                        legend['href'] = re.sub(r'\&access_token=.*', '', legend['href'])

    return model(
        stack_order=ordering,
        format=layer.get("format", None),
        name=layer.get("name", None),
        opacity=layer.get("opacity", 1),
        styles=layer.get("styles", None),
        transparent=layer.get("transparent", False),
        fixed=layer.get("fixed", False),
        group=layer.get('group', None),
        visibility=layer.get("visibility", True),
        ows_url=source.get("url", None),
        layer_params=json.dumps(layer_cfg),
        source_params=json.dumps(source_cfg)
    )


class GXPMapBase(object):

    def viewer_json(self, user, access_token, *added_layers):
        """
        Convert this map to a nested dictionary structure matching the JSON
        configuration for GXP Viewers.

        The ``added_layers`` parameter list allows a list of extra MapLayer
        instances to append to the Map's layer list when generating the
        configuration. These are not persisted; if you want to add layers you
        should use ``.layer_set.create()``.
        """

        if self.id and len(added_layers) == 0:
            cfg = cache.get("viewer_json_" +
                            str(self.id) +
                            "_" +
                            str(0 if user is None else user.id))
            if cfg is not None:
                return cfg

        layers = list(self.layers)
        layers.extend(added_layers)

        server_lookup = {}
        sources = {}

        def uniqify(seq):
            """
            get a list of unique items from the input sequence.

            This relies only on equality tests, so you can use it on most
            things.  If you have a sequence of hashables, list(set(seq)) is
            better.
            """
            results = []
            for x in seq:
                if x not in results:
                    results.append(x)
            return results

        configs = [l.source_config(access_token) for l in layers]

        i = 0
        for source in uniqify(configs):
            while str(i) in sources:
                i = i + 1
            sources[str(i)] = source
            server_lookup[json.dumps(source)] = str(i)

        def source_lookup(source):
            for k, v in sources.iteritems():
                if v == source:
                    return k
            return None

        def layer_config(l, user=None):
            cfg = l.layer_config(user=user)
            src_cfg = l.source_config(access_token)
            source = source_lookup(src_cfg)
            if source:
                cfg["source"] = source
            return cfg

        source_urls = [source['url']
                       for source in sources.values() if 'url' in source]

        if 'geonode.geoserver' in settings.INSTALLED_APPS:
            if len(sources.keys(
            )) > 0 and not settings.MAP_BASELAYERS[0]['source']['url'] in source_urls:
                keys = sorted(sources.keys())
                settings.MAP_BASELAYERS[0]['source'][
                    'title'] = 'Local Geoserver'
                sources[str(int(keys[-1]) + 1)
                        ] = settings.MAP_BASELAYERS[0]['source']

        def _base_source(source):
            base_source = copy.deepcopy(source)
            for key in ["id", "baseParams", "title"]:
                if key in base_source:
                    del base_source[key]
            return base_source

        for idx, lyr in enumerate(settings.MAP_BASELAYERS):
            if _base_source(
                    lyr["source"]) not in map(
                    _base_source,
                    sources.values()):
                if len(sources.keys()) > 0:
                    sources[str(int(max(sources.keys(), key=int)) + 1)
                            ] = lyr["source"]

        # adding remote services sources
        from geonode.services.models import Service
        index = int(max(sources.keys())) if len(sources.keys()) > 0 else 0
        for service in Service.objects.all():
            remote_source = {
                'url': service.base_url,
                'remote': True,
                'ptype': 'gxp_wmscsource',
                'name': service.name
            }
            if remote_source['url'] not in source_urls:
                index += 1
                sources[index] = remote_source

        config = {
            'id': self.id,
            'about': {
                'title': self.title,
                'abstract': self.abstract,
		'introtext' : self.content_map,
		'urlsuffix': self.urlsuffix
            },
            'aboutUrl': '../about',
            'defaultSourceType': "gxp_wmscsource",
            'sources': sources,
            'map': {
                'layers': [layer_config(l, user=user) for l in layers],
                'center': [self.center_x, self.center_y],
                'projection': self.projection,
                'zoom': self.zoom
            }
        }

        if any(layers):
            # Mark the last added layer as selected - important for data page
            config["map"]["layers"][len(layers) - 1]["selected"] = True
        else:
            (def_map_config, def_map_layers) = default_map_config(None)
            config = def_map_config
            layers = def_map_layers

        config["map"].update(_get_viewer_projection_info(self.projection))

        # Create user-specific cache of maplayer config
        if self is not None:
            cache.set("viewer_json_" +
                      str(self.id) +
                      "_" +
                      str(0 if user is None else user.id), config)

        return config


class GXPMap(GXPMapBase):

    def __init__(self, projection=None, title=None, abstract=None,
                 center_x=None, center_y=None, zoom=None, content_map=None,urlsuffix=None):
        self.id = 0
        self.projection = projection
        self.title = title or DEFAULT_TITLE
        self.abstract = abstract or DEFAULT_ABSTRACT
	self.content_map = content_map or DEFAULT_CONTENT
        _DEFAULT_MAP_CENTER = forward_mercator(settings.DEFAULT_MAP_CENTER)
        self.center_x = center_x if center_x is not None else _DEFAULT_MAP_CENTER[
            0]
        self.center_y = center_y if center_y is not None else _DEFAULT_MAP_CENTER[
            1]
        self.zoom = zoom if zoom is not None else settings.DEFAULT_MAP_ZOOM
        self.layers = []
	self.urlsuffix=urlsuffix or DEFAULT_URL


class GXPLayerBase(object):

    def source_config(self, access_token):
        """
        Generate a dict that can be serialized to a GXP layer source
        configuration suitable for loading this layer.
        """
        try:
            cfg = json.loads(self.source_params)
        except Exception:
            cfg = dict(ptype="gxp_wmscsource", restUrl="/gs/rest")

        if self.ows_url:
            '''
            This limits the access token we add to only the OGC servers decalred in OGC_SERVER.
            Will also override any access_token in the request and replace it with an existing one.
            '''
            urls = []
            for name, server in settings.OGC_SERVER.iteritems():
                url = urlparse.urlsplit(server['PUBLIC_LOCATION'])
                urls.append(url.netloc)

            my_url = urlparse.urlsplit(self.ows_url)

            if access_token and my_url.netloc in urls:
                request_params = urlparse.parse_qs(my_url.query)
                if 'access_token' in request_params:
                    del request_params['access_token']
                # request_params['access_token'] = [access_token]
                encoded_params = urllib.urlencode(request_params, doseq=True)

                parsed_url = urlparse.SplitResult(
                    my_url.scheme,
                    my_url.netloc,
                    my_url.path,
                    encoded_params,
                    my_url.fragment)
                cfg["url"] = parsed_url.geturl()
            else:
                cfg["url"] = self.ows_url

        return cfg

    def layer_config(self, user=None):
        """
        Generate a dict that can be serialized to a GXP layer configuration
        suitable for loading this layer.

        The "source" property will be left unset; the layer is not aware of the
        name assigned to its source plugin.  See
        geonode.maps.models.Map.viewer_json for an example of
        generating a full map configuration.
        """
        try:
            cfg = json.loads(self.layer_params)
        except Exception:
            cfg = dict()

        if self.format:
            cfg['format'] = self.format
        if self.name:
            cfg["name"] = self.name
        if self.opacity:
            cfg['opacity'] = self.opacity
        if self.styles:
            cfg['styles'] = self.styles
        if self.transparent:
            cfg['transparent'] = True

        cfg["fixed"] = self.fixed
        if self.group:
            cfg["group"] = self.group
        cfg["visibility"] = self.visibility

        return cfg


class GXPLayer(GXPLayerBase):

    '''GXPLayer represents an object to be included in a GXP map.
    '''

    def __init__(self, name=None, ows_url=None, **kw):
        self.format = None
        self.name = name
        self.opacity = 1.0
        self.styles = None
        self.transparent = False
        self.fixed = False
        self.group = None
        self.visibility = True
        self.wrapDateLine = True
        self.displayOutsideMaxExtent = True
        self.ows_url = ows_url
        self.layer_params = ""
        self.source_params = ""
        for k in kw:
            setattr(self, k, kw[k])


def default_map_config(request):
    if getattr(settings, 'DEFAULT_MAP_CRS', 'EPSG:900913') == "EPSG:4326":
        _DEFAULT_MAP_CENTER = inverse_mercator(settings.DEFAULT_MAP_CENTER)
    else:
        _DEFAULT_MAP_CENTER = forward_mercator(settings.DEFAULT_MAP_CENTER)

    _default_map = GXPMap(
        title=DEFAULT_TITLE,
        abstract=DEFAULT_ABSTRACT,
        projection=getattr(settings, 'DEFAULT_MAP_CRS', 'EPSG:900913'),
        center_x=_DEFAULT_MAP_CENTER[0],
        center_y=_DEFAULT_MAP_CENTER[1],
        zoom=settings.DEFAULT_MAP_ZOOM,
	content_map=DEFAULT_CONTENT
    )

    def _baselayer(lyr, order):
        return layer_from_viewer_config(
            GXPLayer,
            layer=lyr,
            source=lyr["source"],
            ordering=order
        )

    DEFAULT_BASE_LAYERS = [
        _baselayer(
            lyr, idx) for idx, lyr in enumerate(
            settings.MAP_BASELAYERS)]
    user = None
    access_token = None
    if request:
        user = request.user
        if 'access_token' in request.session:
            access_token = request.session['access_token']
        else:
            u = uuid.uuid1()
            access_token = u.hex

    DEFAULT_MAP_CONFIG = _default_map.viewer_json(
        user, access_token, *DEFAULT_BASE_LAYERS)

    return DEFAULT_MAP_CONFIG, DEFAULT_BASE_LAYERS


_viewer_projection_lookup = {
    "EPSG:900913": {
        "maxResolution": 156543.03390625,
        "units": "m",
        "maxExtent": [-20037508.34, -20037508.34, 20037508.34, 20037508.34],
    },
    "EPSG:4326": {
        "max_resolution": (180 - (-180)) / 256,
        "units": "degrees",
        "maxExtent": [-180, -90, 180, 90]
    }
}


def _get_viewer_projection_info(srid):
    # TODO: Look up projection details in EPSG database
    return _viewer_projection_lookup.get(srid, {})


def resolve_object(request, model, query, permission='base.view_resourcebase',
                   permission_required=True, permission_msg=None):
    """Resolve an object using the provided query and check the optional
    permission. Model views should wrap this function as a shortcut.

    query - a dict to use for querying the model
    permission - an optional permission to check
    permission_required - if False, allow get methods to proceed
    permission_msg - optional message to use in 403
    """
    obj = get_object_or_404(model, **query)
    obj_to_check = obj.get_self_resource()

    if settings.RESOURCE_PUBLISHING:
        if (not obj_to_check.is_published) and (
            not request.user.has_perm('publish_resourcebase', obj_to_check)) and (
                not request.user.has_perm('change_resourcebase_metadata', obj_to_check)):
            raise Http404

    allowed = True
    if permission.split('.')[-1] in ['change_layer_data',
                                     'change_layer_style']:
        if obj.__class__.__name__ == 'Layer':
            obj_to_check = obj
    if permission:
        if permission_required or request.method != 'GET':
            allowed = request.user.has_perm(
                permission,
                obj_to_check)
    if not allowed:
        mesg = permission_msg or _('Permission Denied')
        raise PermissionDenied(mesg)
    if settings.MONITORING_ENABLED:
        request.add_resource(model._meta.verbose_name_raw, obj.alternate if hasattr(obj, 'alternate') else obj.title)
    return obj


def json_response(body=None, errors=None, redirect_to=None, exception=None,
                  content_type=None, status=None):
    """Create a proper JSON response. If body is provided, this is the response.
    If errors is not None, the response is a success/errors json object.
    If redirect_to is not None, the response is a success=True, redirect_to object
    If the exception is provided, it will be logged. If body is a string, the
    exception message will be used as a format option to that string and the
    result will be a success=False, errors = body % exception
    """
    if isinstance(body, HttpResponse):
        return body
    if content_type is None:
        content_type = "application/json"
    if errors:
        if isinstance(errors, basestring):
            errors = [errors]
        body = {
            'success': False,
            'errors': errors
        }
    elif redirect_to:
        body = {
            'success': True,
            'redirect_to': redirect_to
        }
    elif exception:
        if body is None:
            body = "Unexpected exception %s" % exception
        else:
            body = body % exception
        body = {
            'success': False,
            'errors': [body]
        }
    elif body:
        pass
    else:
        raise Exception("must call with body, errors or redirect_to")

    if status is None:
        status = 200

    if not isinstance(body, basestring):
        body = json.dumps(body, cls=DjangoJSONEncoder)
    return HttpResponse(body, content_type=content_type, status=status)


def num_encode(n):
    if n < 0:
        return SIGN_CHARACTER + num_encode(-n)
    s = []
    while True:
        n, r = divmod(n, BASE)
        s.append(ALPHABET[r])
        if n == 0:
            break
    return ''.join(reversed(s))


def num_decode(s):
    if s[0] == SIGN_CHARACTER:
        return -num_decode(s[1:])
    n = 0
    for c in s:
        n = n * BASE + ALPHABET_REVERSE[c]
    return n


def format_urls(a, values):
    b = []
    for i in a:
        j = i.copy()
        try:
            j['url'] = unicode(j['url']).format(**values)
        except KeyError:
            j['url'] = None
        b.append(j)
    return b


def build_abstract(resourcebase, url=None, includeURL=True):
    if resourcebase.abstract and url and includeURL:
        return u"{abstract} -- [{url}]({url})".format(
            abstract=resourcebase.abstract, url=url)
    else:
        return resourcebase.abstract


def build_caveats(resourcebase):
    caveats = []
    if resourcebase.maintenance_frequency:
        caveats.append(resourcebase.maintenance_frequency_title())
    if resourcebase.license:
        caveats.append(resourcebase.license_verbose)
    if resourcebase.data_quality_statement:
        caveats.append(resourcebase.data_quality_statement)
    if len(caveats) > 0:
        return u"- " + u"%0A- ".join(caveats)
    else:
        return u""


def build_social_links(request, resourcebase):
    social_url = u"{protocol}://{host}{path}".format(
        protocol=("https" if request.is_secure() else "http"),
        host=request.get_host(),
        path=request.get_full_path())
    # Don't use datetime strftime() because it requires year >= 1900
    # see
    # https://docs.python.org/2/library/datetime.html#strftime-strptime-behavior
    date = '{0.month:02d}/{0.day:02d}/{0.year:4d}'.format(
        resourcebase.date) if resourcebase.date else None
    abstract = build_abstract(resourcebase, url=social_url, includeURL=True)
    caveats = build_caveats(resourcebase)
    hashtags = ",".join(getattr(settings, 'TWITTER_HASHTAGS', []))
    return format_urls(
        settings.SOCIAL_ORIGINS,
        {
            'name': resourcebase.title,
            'date': date,
            'abstract': abstract,
            'caveats': caveats,
            'hashtags': hashtags,
            'url': social_url})


def check_shp_columnnames(layer):
    """ Check if shapefile for a given layer has valid column names.
        If not, try to fix column names and warn the user
    """

    # TODO we may add in a better location this method
    if layer.charset is u"":
        layer.charset = unicode('UTF-8');

    inShapefile = ''
    for f in layer.upload_session.layerfile_set.all():
        if os.path.splitext(f.file.name)[1] == '.shp':
            inShapefile = f.file.path

    inDriver = ogr.GetDriverByName('ESRI Shapefile')
    inDataSource = inDriver.Open(inShapefile, 1)
    if inDataSource is None:
        print 'Could not open %s' % (inShapefile)
        return False, None, None
    else:
        inLayer = inDataSource.GetLayer()

    # TODO we may need to improve this regexp
    # first character must be any letter or "_"
    # following characters can be any letter, number, "#", ":"
    regex = r'^[a-zA-Z,_][a-zA-Z,_,#,:\d]*$'
    a = re.compile(regex)
    regex_first_char = r'[a-zA-Z,_]{1}'
    b = re.compile(regex_first_char)
    inLayerDefn = inLayer.GetLayerDefn()

    list_col_original = []
    list_col = {}

    for i in range(0, inLayerDefn.GetFieldCount()):
        field_name = inLayerDefn.GetFieldDefn(i).GetName()

        if a.match(field_name):
            list_col_original.append(field_name)
    try:
        for i in range(0, inLayerDefn.GetFieldCount()):
            field_name = unicode(
                inLayerDefn.GetFieldDefn(i).GetName(),
                layer.charset)

            if not a.match(field_name):
                new_field_name = custom_slugify(field_name)

                if not b.match(new_field_name):
                    new_field_name = '_' + new_field_name
                j = 0
                while new_field_name in list_col_original or new_field_name in list_col.values():
                    if j == 0:
                        new_field_name += '_0'
                    if new_field_name.endswith('_' + str(j)):
                        j += 1
                        new_field_name = new_field_name[:-2] + '_' + str(j)
                list_col.update({field_name: new_field_name})
    except UnicodeDecodeError as e:
        print str(e)
        return False, None, None

    if len(list_col) == 0:
        return True, None, None
    else:
        for key in list_col.keys():
            qry = u"ALTER TABLE {0} RENAME COLUMN \"{1}\" TO \"{2}\"".format(
                inLayer.GetName(), key, list_col[key])
            inDataSource.ExecuteSQL(qry.encode(layer.charset))
    return True, None, list_col


def set_attributes(
        layer,
        attribute_map,
        overwrite=False,
        attribute_stats=None):
    """ *layer*: a geonode.layers.models.Layer instance
        *attribute_map*: a list of 2-lists specifying attribute names and types,
            example: [ ['id', 'Integer'], ... ]
        *overwrite*: replace existing attributes with new values if name/type matches.
        *attribute_stats*: dictionary of return values from get_attribute_statistics(),
            of the form to get values by referencing attribute_stats[<layer_name>][<field_name>].
    """
    # Some import dependency tweaking; functions in this module are used before
    # models are fully set up so Attribute has to be imported here.
    from geonode.layers.models import Attribute

    # we need 3 more items; description, attribute_label, and display_order
    attribute_map_dict = {
        'field': 0,
        'ftype': 1,
        'description': 2,
        'label': 3,
        'display_order': 4,
    }
    for attribute in attribute_map:
        attribute.extend((None, None, 0))

    attributes = layer.attribute_set.all()
    # Delete existing attributes if they no longer exist in an updated layer
    for la in attributes:
        lafound = False
        for attribute in attribute_map:
            field, ftype, description, label, display_order = attribute
            if field == la.attribute:
                lafound = True
                # store description and attribute_label in attribute_map
                attribute[attribute_map_dict['description']] = la.description
                attribute[attribute_map_dict['label']] = la.attribute_label
                attribute[attribute_map_dict['display_order']
                          ] = la.display_order
        if overwrite or not lafound:
            logger.debug(
                "Going to delete [%s] for [%s]",
                la.attribute,
                layer.name.encode('utf-8'))
            la.delete()

    # Add new layer attributes if they don't already exist
    if attribute_map is not None:
        iter = len(Attribute.objects.filter(layer=layer)) + 1
        for attribute in attribute_map:
            field, ftype, description, label, display_order = attribute
            if field is not None:
                la, created = Attribute.objects.get_or_create(
                    layer=layer, attribute=field, attribute_type=ftype,
                    description=description, attribute_label=label,
                    display_order=display_order)
                if created:
                    if (not attribute_stats or layer.name not in attribute_stats or
                            field not in attribute_stats[layer.name]):
                        result = None
                    else:
                        result = attribute_stats[layer.name][field]

                    if result is not None:
                        logger.debug("Generating layer attribute statistics")
                        la.count = result['Count']
                        la.min = result['Min']
                        la.max = result['Max']
                        la.average = result['Average']
                        la.median = result['Median']
                        la.stddev = result['StandardDeviation']
                        la.sum = result['Sum']
                        la.unique_values = result['unique_values']
                        la.last_stats_updated = datetime.datetime.now()
                    la.visible = ftype.find("gml:") != 0
                    la.display_order = iter
                    la.save()
                    iter += 1
                    logger.debug(
                        "Created [%s] attribute for [%s]",
                        field,
                        layer.name.encode('utf-8'))
    else:
        logger.debug("No attributes found")


def id_to_obj(id_):
    if id_ == id_none:
        return None

    for obj in gc.get_objects():
        if id(obj) == id_:
            return obj
            break
    raise Exception("Not found")


def printsignals():
    for signalname in signalnames:
        logger.debug("SIGNALNAME: %s" % signalname)
        signaltype = getattr(models.signals, signalname)
        signals = signaltype.receivers[:]
        for signal in signals:
            logger.info(signal)


def designals():
    global signals_store

    for signalname in signalnames:
        signaltype = getattr(models.signals, signalname)
        logger.debug("RETRIEVE: %s: %d" %
                     (signalname, len(signaltype.receivers)))
        signals_store[signalname] = []
        signals = signaltype.receivers[:]
        for signal in signals:
            uid = receiv_call = None
            sender_ista = sender_call = None
            # first tuple element:
            # - case (id(instance), id(method))
            if not isinstance(signal[0], tuple):
                raise "Malformed signal"

            lookup = signal[0]

            if isinstance(lookup[0], tuple):
                # receiv_ista = id_to_obj(lookup[0][0])
                receiv_call = id_to_obj(lookup[0][1])
            else:
                # - case id(function) or uid
                try:
                    receiv_call = id_to_obj(lookup[0])
                except BaseException:
                    uid = lookup[0]

            if isinstance(lookup[1], tuple):
                sender_call = id_to_obj(lookup[1][0])
                sender_ista = id_to_obj(lookup[1][1])
            else:
                sender_ista = id_to_obj(lookup[1])

            # second tuple element
            if (isinstance(signal[1], weakref.ReferenceType)):
                is_weak = True
                receiv_call = signal[1]()
            else:
                is_weak = False
                receiv_call = signal[1]

            signals_store[signalname].append({
                'uid': uid, 'is_weak': is_weak,
                'sender_ista': sender_ista, 'sender_call': sender_call,
                'receiv_call': receiv_call,
            })

            signaltype.disconnect(
                receiver=receiv_call,
                sender=sender_ista,
                weak=is_weak,
                dispatch_uid=uid)


def resignals():
    global signals_store

    for signalname in signalnames:
        signals = signals_store[signalname]
        signaltype = getattr(models.signals, signalname)
        for signal in signals:
            signaltype.connect(
                signal['receiv_call'],
                sender=signal['sender_ista'],
                weak=signal['is_weak'],
                dispatch_uid=signal['uid'])


def run_subprocess(*cmd, **kwargs):
    p = subprocess.Popen(
        ' '.join(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **kwargs)
    stdout = StringIO()
    stderr = StringIO()
    buff_size = 1024
    while p.poll() is None:
        inr = [p.stdout.fileno(), p.stderr.fileno()]
        inw = []
        rlist, wlist, xlist = select.select(inr, inw, [])

        for r in rlist:
            if r == p.stdout.fileno():
                readfrom = p.stdout
                readto = stdout
            else:
                readfrom = p.stderr
                readto = stderr
            readto.write(readfrom.read(buff_size))

        for w in wlist:
            w.write('')

    return p.returncode, stdout.getvalue(), stderr.getvalue()


class WorldmapDatabaseRouter(object):
    """A router to control all database operations on models in
    the gazetteer application"""

    apps = ['gazetteer']

    def db_for_read(self, model, **hints):
        """Point all operations on gazetteer models to gazetteer db"""
        if model._meta.app_label in self.apps:
            return settings.GAZETTEER_DB_ALIAS
        return None

    def db_for_write(self, model, **hints):
        """Point all operations on gazetteer models to gazetteer db"""
        if model._meta.app_label in self.apps:
            return settings.GAZETTEER_DB_ALIAS
        return None

    def allow_relation(self, obj1, obj2, **hints):
        """Allow any relation if a model in gazetteer is involved"""
        if obj1._meta.app_label in self.apps or obj2._meta.app_label in self.apps:
            return True
        return None

    def allow_syncdb(self, db, model):
        """Make sure the gazetteer app only appears on the gazetteer db"""
        if model._meta.app_label in ['south']:
            return True
        if db == settings.GAZETTEER_DB_ALIAS:
            return model._meta.app_label in self.apps
        elif model._meta.app_label in self.apps:
            return False
        return None

    def allow_migrate(self, db, model):
        """Make sure the gazetteer app only appears on the gazetteer db"""
        if model._meta.app_label in ['south']:
            return True
        if db == settings.GAZETTEER_DB_ALIAS:
            return model._meta.app_label in self.apps
        elif model._meta.app_label in self.apps:
            return False
        return None

def parse_datetime(value):
    for patt in settings.DATETIME_INPUT_FORMATS:
        try:
            return datetime.datetime.strptime(value, patt)
        except ValueError:
            pass
    raise ValueError("Invalid datetime input: {}".format(value))


def _convert_sql_params(cur, query):
    # sqlite driver doesn't support %(key)s notation,
    # use :key instead.
    if cur.db.vendor in ('sqlite', 'sqlite3', 'spatialite',):
        return SQL_PARAMS_RE.sub(r':\1', query)
    return query


@transaction.atomic
def raw_sql(query, params=None, ret=True):
    """
    Execute raw query
    param ret=True returns data from cursor as iterator
    """
    with connection.cursor() as c:
        query = _convert_sql_params(c, query)
        c.execute(query, params)
        if ret:
            desc = [r[0] for r in c.description]
            for row in c:
                yield dict(zip(desc, row))


def check_ogc_backend(backend_package):
    """Check that geonode use a particular OGC Backend integration

    :param backend_package: django app of backend to use
    :type backend_package: str

    :return: bool
    :rtype: bool
    """
    # Check exists in INSTALLED_APPS
    try:
        in_installed_apps = backend_package in settings.INSTALLED_APPS
        ogc_conf = settings.OGC_SERVER['default']
        is_configured = ogc_conf.get('BACKEND') == backend_package
        return in_installed_apps and is_configured
    except:
        return False
