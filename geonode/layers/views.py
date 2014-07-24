# -*- coding: utf-8 -*-
#########################################################################
#
# Copyright (C) 2012 OpenPlans
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

import os
import logging
import shutil

from django.contrib.auth.decorators import login_required
from django.core.urlresolvers import reverse
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render_to_response
from django.conf import settings
from django.template import RequestContext
from django.utils.translation import ugettext as _
from django.utils import simplejson as json
from django.utils.html import escape
from django.template.defaultfilters import slugify
from django.forms.models import inlineformset_factory
from geonode.services.models import Service

from geonode.layers.forms import LayerForm, LayerUploadForm, NewLayerUploadForm, LayerAttributeForm
from geonode.base.forms import CategoryForm
from geonode.layers.models import Layer, Attribute
from geonode.base.enumerations import CHARSETS
from geonode.base.models import TopicCategory

from geonode.utils import default_map_config, llbbox_to_mercator
from geonode.utils import GXPLayer
from geonode.utils import GXPMap
from geonode.layers.utils import file_upload
from geonode.layers.data import layer_sos, layer_netcdf
from geonode.utils import resolve_object
from geonode.people.forms import ProfileForm, PocForm
from geonode.security.views import _perms_info_json
from geonode.documents.models import get_related_documents

logger = logging.getLogger("geonode.layers.views")

DEFAULT_SEARCH_BATCH_SIZE = 10
MAX_SEARCH_BATCH_SIZE = 25
GENERIC_UPLOAD_ERROR = _("There was an error while attempting to upload your data. \
Please try again, or contact and administrator if the problem continues.")

_PERMISSION_MSG_DELETE = _("You are not permitted to delete this layer")
_PERMISSION_MSG_GENERIC = _('You do not have permissions for this layer.')
_PERMISSION_MSG_MODIFY = _("You are not permitted to modify this layer")
_PERMISSION_MSG_METADATA = _("You are not permitted to modify this layer's metadata")
_PERMISSION_MSG_VIEW = _("You are not permitted to view this layer")


def _resolve_layer(request, typename, permission='base.view_resourcebase',
                   msg=_PERMISSION_MSG_GENERIC, **kwargs):
    """
    Resolve the layer by the provided typename (which may include service name) and check the optional permission.
    """
    service_typename = typename.split(":",1)
    service = Service.objects.filter(name=service_typename[0])

    if service.count() > 0 and service[0].method != "C":
        return resolve_object(request, Layer, {'service': service[0], 'typename':service_typename[1]},
                              permission = permission, permission_msg=msg, **kwargs)
    else:
        return resolve_object(request, Layer, {'typename':typename},
                          permission = permission, permission_msg=msg, **kwargs)


#### Basic Layer Views ####


@login_required
def layer_upload(request, template='upload/layer_upload.html'):
    if request.method == 'GET':
        ctx = {
            'charsets': CHARSETS
        }
        return render_to_response(template,
                                  RequestContext(request, ctx))
    elif request.method == 'POST':
        form = NewLayerUploadForm(request.POST, request.FILES)
        tempdir = None
        errormsgs = []
        out = {'success': False}

        if form.is_valid():
            title = form.cleaned_data["layer_title"]

            # Replace dots in filename - GeoServer REST API upload bug
            # and avoid any other invalid characters.
            # Use the title if possible, otherwise default to the filename
            if title is not None and len(title) > 0:
                name_base = title
            else:
                name_base, __ = os.path.splitext(form.cleaned_data["base_file"].name)

            name = slugify(name_base.replace(".","_"))

            try:
                # Moved this inside the try/except block because it can raise
                # exceptions when unicode characters are present.
                # This should be followed up in upstream Django.
                tempdir, base_file = form.write_files()
                saved_layer = file_upload(base_file,
                        name=name,
                        user=request.user,
                        overwrite = False,
                        charset = form.cleaned_data["charset"],
                        abstract = form.cleaned_data["abstract"],
                        title = form.cleaned_data["layer_title"],
                        )

            except Exception, e:
                logger.exception(e)
                out['success'] = False
                out['errors'] = str(e)
            else:
                out['success'] = True
                out['url'] = reverse('layer_detail', args=[saved_layer.service_typename])

                permissions = form.cleaned_data["permissions"]
                if permissions is not None and len(permissions.keys()) > 0:
                    saved_layer.set_permissions(permissions)

            finally:
                if tempdir is not None:
                    shutil.rmtree(tempdir)
        else:
            for e in form.errors.values():
                errormsgs.extend([escape(v) for v in e])

            out['errors'] = form.errors
            out['errormsgs'] = errormsgs

        if out['success']:
            status_code = 200
        else:
            status_code = 500
        return HttpResponse(json.dumps(out), mimetype='application/json', status=status_code)


def layer_detail(request, layername, template='layers/layer_detail.html'):

    layer = _resolve_layer(request, layername, 'base.view_resourcebase', _PERMISSION_MSG_VIEW)
    layer_bbox = layer.bbox
    # assert False, str(layer_bbox)
    bbox = list(layer_bbox[0:4])
    config = layer.attribute_config()

    #Add required parameters for GXP lazy-loading
    config["srs"] = layer.srid
    config["title"] = layer.title
    config["bbox"] =  [float(coord) for coord in bbox] \
        if layer.srid == "EPSG:4326" else llbbox_to_mercator([float(coord) for coord in bbox])

    if layer.storeType == "remoteStore":
        service = layer.service
        source_params = {"ptype":service.ptype, "remote": True, "url": service.base_url, "name": service.name}
        maplayer = GXPLayer(name = layer.typename, ows_url = layer.ows_url, layer_params=json.dumps( config),
                            source_params=json.dumps(source_params))
    else:
        maplayer = GXPLayer(name = layer.typename, ows_url = layer.ows_url, layer_params=json.dumps( config))

    # Update count for popularity ranking.
    Layer.objects.filter(id=layer.id).update(popular_count=layer.popular_count +1)

    # center/zoom don't matter; the viewer will center on the layer bounds
    map_obj = GXPMap(projection="EPSG:900913")
    NON_WMS_BASE_LAYERS = [la for la in default_map_config()[1] if la.ows_url is None]

    metadata = layer.link_set.metadata().filter(
        name__in=settings.DOWNLOAD_FORMATS_METADATA)

    context_dict = {
        "resource": layer,
        "permissions_json": _perms_info_json(layer),
        "documents": get_related_documents(layer),
        "metadata": metadata,
    }

    context_dict["viewer"] = json.dumps(map_obj.viewer_json(request.user, * (NON_WMS_BASE_LAYERS + [maplayer])))
    context_dict["preview"] = getattr(settings, 'LAYER_PREVIEW_LIBRARY', 'leaflet')

    if layer.storeType=='dataStore':
        links = layer.link_set.download().filter(
        name__in=settings.DOWNLOAD_FORMATS_VECTOR)
    else:
        links = layer.link_set.download().filter(
        name__in=settings.DOWNLOAD_FORMATS_RASTER)


    context_dict["links"] = links

    keys = [lkw.name for lkw in layer.keywords.all()]
    context_dict["keys"] = keys

    return render_to_response(template, RequestContext(request, context_dict))


@login_required
def layer_metadata(request, layername, template='layers/layer_metadata.html'):
    layer = _resolve_layer(request, layername, 'base.change_resourcebase', _PERMISSION_MSG_METADATA)
    layer_attribute_set = inlineformset_factory(Layer, Attribute, extra=0, form=LayerAttributeForm, )
    topic_category = layer.category

    poc = layer.poc
    metadata_author = layer.metadata_author

    if request.method == "POST":
        layer_form = LayerForm(request.POST, instance=layer, prefix="resource")
        attribute_form = layer_attribute_set(request.POST, instance=layer, prefix="layer_attribute_set", queryset=Attribute.objects.order_by('display_order'))
        category_form = CategoryForm(request.POST,prefix="category_choice_field",
            initial=int(request.POST["category_choice_field"]) if "category_choice_field" in request.POST else None)
    else:
        layer_form = LayerForm(instance=layer, prefix="resource")
        attribute_form = layer_attribute_set(instance=layer, prefix="layer_attribute_set", queryset=Attribute.objects.order_by('display_order'))
        category_form = CategoryForm(prefix="category_choice_field", initial=topic_category.id if topic_category else None)

    if request.method == "POST" and layer_form.is_valid() and attribute_form.is_valid() and category_form.is_valid():
        new_poc = layer_form.cleaned_data['poc']
        new_author = layer_form.cleaned_data['metadata_author']
        new_keywords = layer_form.cleaned_data['keywords']

        if new_poc is None:
            if poc is None:
                poc_form = ProfileForm(request.POST, prefix="poc", instance=poc)
            else:
                poc_form = ProfileForm(request.POST, prefix="poc")
            if poc_form.has_changed and poc_form.is_valid():
                new_poc = poc_form.save()

        if new_author is None:
            if metadata_author is None:
                author_form = ProfileForm(request.POST, prefix="author",
                    instance=metadata_author)
            else:
                author_form = ProfileForm(request.POST, prefix="author")
            if author_form.has_changed and author_form.is_valid():
                new_author = author_form.save()

        new_category = TopicCategory.objects.get(id=category_form.cleaned_data['category_choice_field'])

        for form in attribute_form.cleaned_data:
            la = Attribute.objects.get(id=int(form['id'].id))
            la.description = form["description"]
            la.attribute_label = form["attribute_label"]
            la.visible = form["visible"]
            la.display_order = form["display_order"]
            la.save()

        if new_poc is not None and new_author is not None:
            the_layer = layer_form.save()
            the_layer.poc = new_poc
            the_layer.metadata_author = new_author
            the_layer.keywords.clear()
            the_layer.keywords.add(*new_keywords)
            the_layer.category = new_category
            the_layer.save()
            return HttpResponseRedirect(reverse('layer_detail', args=(layer.service_typename,)))

    if poc is None:
        poc_form = ProfileForm(instance=poc, prefix="poc")
    else:
        layer_form.fields['poc'].initial = poc.id
        poc_form = ProfileForm(prefix="poc")
        poc_form.hidden=True

    if metadata_author is None:
        author_form = ProfileForm(instance=metadata_author, prefix="author")
    else:
        layer_form.fields['metadata_author'].initial = metadata_author.id
        author_form = ProfileForm(prefix="author")
        author_form.hidden=True

    return render_to_response(template, RequestContext(request, {
        "layer": layer,
        "layer_form": layer_form,
        "poc_form": poc_form,
        "author_form": author_form,
        "attribute_form": attribute_form,
        "category_form": category_form,
    }))



@login_required
def layer_change_poc(request, ids, template = 'layers/layer_change_poc.html'):
    layers = Layer.objects.filter(id__in=ids.split('_'))
    if request.method == 'POST':
        form = PocForm(request.POST)
        if form.is_valid():
            for layer in layers:
                layer.poc = form.cleaned_data['contact']
                layer.save()
            # Process the data in form.cleaned_data
            # ...
            return HttpResponseRedirect('/admin/maps/layer') # Redirect after POST
    else:
        form = PocForm() # An unbound form
    return render_to_response(template, RequestContext(request,
                                  {'layers': layers, 'form': form }))


@login_required
def layer_replace(request, layername, template='layers/layer_replace.html'):
    layer = _resolve_layer(request, layername, 'base.change_resourcebase',_PERMISSION_MSG_MODIFY)

    if request.method == 'GET':
        return render_to_response(template,
                                  RequestContext(request, {'layer': layer,
                                                           'is_featuretype': layer.is_vector()}))
    elif request.method == 'POST':

        form = LayerUploadForm(request.POST, request.FILES)
        tempdir = None
        out = {}

        if form.is_valid():
            try:
                tempdir, base_file = form.write_files()
                saved_layer = file_upload(base_file, name=layer.name,
                                          user=request.user, overwrite=True)
            except Exception, e:
                out['success'] = False
                out['errors'] = str(e)
            else:
                out['success'] = True
                out['url'] = reverse('layer_detail', args=[saved_layer.service_typename])
            finally:
                if tempdir is not None:
                    shutil.rmtree(tempdir)
        else:
            for e in form.errors.values():
                errormsgs.extend([escape(v) for v in e])

            out['errors'] = form.errors
            out['errormsgs'] = errormsgs

        if out['success']:
            status_code = 200
        else:
            status_code = 500
        return HttpResponse(json.dumps(out), mimetype='application/json', status=status_code)

@login_required
def layer_remove(request, layername, template='layers/layer_remove.html'):
    try:
        layer = _resolve_layer(request, layername, 'base.delete_resourcebase',
                               _PERMISSION_MSG_DELETE)

        if (request.method == 'GET'):
            return render_to_response(template,RequestContext(request, {
                "layer": layer
            }))
        if (request.method == 'POST'):
            layer.delete()
            return HttpResponseRedirect(reverse("layer_browse"))
        else:
            return HttpResponse("Not allowed",status=403)
    except PermissionDenied:
        return HttpResponse(
                'You are not allowed to delete this layer',
                mimetype="text/plain",
                status=401
        )

#### Optional Layer Views ####


def layer_data(request, layername, mimetype="text/csv"):
    """Return non-spatial data for a named layer, in required mimetype format.

    Access to the layer keywords is needed to determine the additional
    characteristics defined for a layer.

    Access to the layer's supplemental information at this level must be
    parsed to route the request to the correct function.
    """
    layer = _resolve_layer(
        request, layername, 'layers.view_layer', _PERMISSION_MSG_VIEW)
    if 'feature' in request.GET:
        feature = request.GET['feature']
    else:
        feature = None
    keys = [lkw.name for lkw in layer.keywords.all()]
    sup_inf_str = str(layer.supplemental_information)
    if "SOS" in keys:
        return layer_sos(feature, sup_inf_str, time=None, mimetype=mimetype)
    elif "NetCDF" in keys:
        return layer_netcdf(request, layername, time=None, mimetype=mimetype)
    else:
        pass
    return None