# Copyright: 2012 MoinMoin:CheerXiao
# Copyright: 2009 MoinMoin:ThomasWaldmann
# Copyright: 2009-2011 MoinMoin:ReimarBauer
# Copyright: 2009 MoinMoin:ChristopherDenter
# Copyright: 2008,2009 MoinMoin:BastianBlank
# Copyright: 2010 MoinMoin:ValentinJaniaut
# Copyright: 2010 MoinMoin:DiogenesAugusto
# License: GNU GPL v2 (or any later version), see LICENSE.txt for details.

"""
    MoinMoin - high-level (frontend) items

    While moin.storage cares for backend storage of items,
    this module cares for more high-level, frontend items,
    e.g. showing, editing, etc. of wiki items.

    Each class in this module corresponds to an itemtype.
"""

from time import time, strftime
import json
from io import BytesIO
from collections import namedtuple
from operator import attrgetter
import re

from flask import current_app as app
from flask import g as flaskg
from flask import request, Response, redirect, abort, url_for, flash

from flatland import Form
from flatland.validation import Validator

from jinja2 import Markup

from whoosh.query import Term, Prefix, And, Or, Not

from moin.constants.contenttypes import CONTENTTYPES_HELP_DOCS
from moin.constants.misc import NO_LOCK, LOCKED, LOCK
from moin.signalling import item_modified
from moin.storage.middleware.protecting import AccessDenied
from moin.i18n import _, L_, N_
from moin.themes import render_template
from moin.utils import rev_navigation, close_file, show_time
from moin.utils.edit_locking import Edit_Utils
from moin.utils.mime import Type
from moin.utils.interwiki import url_for_item, split_fqname, get_fqname, CompositeName
from moin.utils.registry import RegistryBase
from moin.utils.clock import timed
from moin.utils.diff_html import diff as html_diff
from moin.utils import diff3
from moin.forms import RequiredText, OptionalText, JSON, Tags, Names
from moin.constants.keys import (
    NAME, NAME_OLD, NAME_EXACT, WIKINAME, MTIME, ITEMTYPE,
    CONTENTTYPE, SIZE, ACTION, ADDRESS, HOSTNAME, USERID, COMMENT, USERGROUP,
    HASH_ALGORITHM, ITEMID, REVID, DATAID, CURRENT, PARENTID, NAMESPACE, IMMUTABLE_KEYS,
    UFIELDS_TYPELIST, UFIELDS, TRASH, REV_NUMBER,
    ACTION_SAVE, ACTION_REVERT, ACTION_TRASH, ACTION_RENAME, TAGS, TEMPLATE,
    LATEST_REVS, EDIT_ROWS
)
from moin.constants.namespaces import NAMESPACE_ALL
from moin.constants.contenttypes import CHARSET, CONTENTTYPE_NONEXISTENT, CONTENTTYPE_VARIABLES
from moin.constants.itemtypes import (
    ITEMTYPE_NONEXISTENT, ITEMTYPE_USERPROFILE, ITEMTYPE_DEFAULT, ITEMTYPE_TICKET
)
from moin.utils.notifications import DESTROY_REV, DESTROY_ALL
from moin.mail.sendmail import encodeSpamSafeEmail

from .content import content_registry, Content, NonExistentContent, Draw, Text
from ..utils.pysupport import load_package_modules

from moin import log
logging = log.getLogger(__name__)


COLS = 80
ROWS_META = 10


def _verify_parents(self, new_name, namespace, old_name=''):
    """
    If this is a subitem, verify all parent items exist. Return None if OK, raise error if not OK.
    """
    name_segments = new_name.split('/')
    for idx in range(len(name_segments) - 1):
        root_name = '/'.join(name_segments[:idx + 1])
        fqname = CompositeName(namespace, NAME_EXACT, root_name)
        parent_item = flaskg.unprotected_storage.get_item(**fqname.query)
        if parent_item.itemid is None:
            raise MissingParentError(_("Cannot create or rename item '%(new_name)s' because parent '%(parent_name)s' is missing.", new_name=new_name, parent_name=name_segments[idx]))


class RegistryItem(RegistryBase):
    class Entry(namedtuple('Entry', 'factory itemtype display_name description order')):
        def __call__(self, itemtype, *args, **kw):
            if self.itemtype == itemtype:
                return self.factory(*args, **kw)

        def __lt__(self, other):
            if isinstance(other, self.__class__):
                return self.itemtype < other.itemtype
            return NotImplemented

    def __init__(self):
        super(RegistryItem, self).__init__()
        self.shown_entries = []

    def register(self, e, shown):
        """
        Register a factory

        :param factory: Factory to register. Callable, must return an object.
        """
        if shown:
            self.shown_entries.append(e)
            self.shown_entries.sort(key=attrgetter('order'))
        return self._register(e)


item_registry = RegistryItem()


def register(cls):
    item_registry.register(RegistryItem.Entry(cls._factory, cls.itemtype, cls.display_name, cls.description, cls.order),
                           cls.shown)
    return cls


class DummyRev(dict):
    """ if we have no stored Revision, we use this dummy """
    def __init__(self, item, itemtype=None, contenttype=None):
        self.item = item
        fqname = item.fqname
        self.meta = {
            ITEMTYPE: itemtype or ITEMTYPE_NONEXISTENT,
            CONTENTTYPE: contenttype or CONTENTTYPE_NONEXISTENT
        }
        self.data = BytesIO(b'')
        self.revid = None
        if item:
            self.meta[NAMESPACE] = fqname.namespace
            if fqname.field in UFIELDS_TYPELIST:
                if fqname.field == NAME_EXACT:
                    self.meta[NAME] = [fqname.value]
                else:
                    self.meta[fqname.field] = [fqname.value]
            else:
                self.meta[fqname.field] = fqname.value


class DummyItem(object):
    """ if we have no stored Item, we use this dummy """
    def __init__(self, fqname):
        self.fqname = fqname

    def list_revisions(self):
        return []  # same as an empty Item

    def destroy_all_revisions(self):
        return True


def get_storage_revision(fqname, itemtype=None, contenttype=None, rev_id=CURRENT, item=None):
    """
    Get a storage Revision.

    If :item is supplied it is used as the storage Item; otherwise the storage
    Item is looked up with :name. If it is not found (either because the item
    doesn't exist or the user does not have the required permissions) a
    DummyItem is created, and a DummyRev is created with appropriate metadata
    properties and the "item" property pointing to the DummyItem. The DummyRev
    is then returned.

    If the previous step didn't end up with a DummyRev, the revision
    designated by :rev_id is then looked up. If it is not found, current
    revision is looked up and returned instead. If current revision is not
    found (i.e. the item has no revision), a DummyRev is created. (TODO: in
    the last two cases, emit warnings or throw exceptions.)

    :itemtype and :contenttype are used when creating a DummyRev, where
    metadata is not available from the storage.
    """
    rev_id = fqname.value if fqname.field == REVID else rev_id
    if 1:  # try:
        if item is None:
            item = flaskg.storage.get_item(**fqname.query)
        else:
            if item.fqname:
                fqname = item.fqname
    if not item:  # except NoSuchItemError:
        logging.debug("No such item: {0!r}".format(fqname))
        item = DummyItem(fqname)
        rev = DummyRev(item, itemtype, contenttype)
        logging.debug("Item {0!r}, created dummy revision with contenttype {1!r}".format(fqname, contenttype))
    else:
        logging.debug("Got item: {0!r}".format(fqname))
        try:
            rev = item.get_revision(rev_id)
        except KeyError:  # NoSuchRevisionError:
            try:
                rev = item.get_revision(CURRENT)  # fall back to current revision
                # XXX add some message about invalid revision
            except KeyError:  # NoSuchRevisionError:
                logging.debug("Item {0!r} has no revisions.".format(fqname))
                rev = DummyRev(item, itemtype, contenttype)
                logging.debug("Item {0!r}, created dummy revision with contenttype {1!r}".format(fqname, contenttype))
        logging.debug("Got item {0!r}, revision: {1!r}".format(fqname, rev_id))
    return rev


class BaseChangeForm(Form):
    # autofocus=True causes javascript autoscroll in textarea to fail when using Chrome, Opera, or Maxthon browsers
    comment = OptionalText.using(label=L_('Comment')).with_properties(placeholder=L_("Comment about your change"), autofocus=False)
    submit_label = L_('OK')
    preview_label = L_('Preview')
    cancel_label = L_('Cancel')


class CreateItemForm(BaseChangeForm):
    target = RequiredText.using(label=L_('Target')).with_properties(autofocus=True)


def acl_validate(acl_string):
    """
    Validate ACL strings, allowing special values 'None' and 'Empty'.

    In later processes, None means no item ACLs, so the configured default ACLs will be used.
    Empty is same as "". If there are no configured 'after' ACLs, then Empty and "" are equivalent to "All:".
    """
    all_rights = set(('read', 'write', 'create', 'destroy', 'admin'))
    acls = unicode(acl_string)
    if acls in (u'None', u'Empty', u''):  # u'' is not possible if field is required on form
        return True
    acls = acls.split()
    for acl in acls:
        acl_rules = acl.split(':')
        if len(acl_rules) == 2:
            who, rights = acl_rules
            if rights:
                rights = rights.split(',')
                for right in rights:
                    if right not in all_rights:
                        return False
        else:
            return False
    return True


class ACLValidator(Validator):
    """
    Meta Validator - currently used for validating ACLs only
    """
    acl_fail_msg = L_("The ACL string is invalid.")

    def validate(self, element, state):
        if acl_validate(element) is True:
            return True
        flash(L_("The ACL string is invalid."), "error")
        return self.note_error(element, state, 'acl_fail_msg')


class BaseMetaForm(Form):

    itemtype = RequiredText.using(label=L_("Item type")).with_properties(placeholder=L_("Item type"))
    contenttype = RequiredText.using(label=L_("Content type")).with_properties(placeholder=L_("Content type"))
    # Flatland doesn't distinguish between empty value and nonexistent value, use None for noneexistent and Empty for empty
    acl = RequiredText.using(label=L_("ACL")).with_properties(placeholder=L_("Access Control List - Use 'None' for default")).validated_by(ACLValidator())
    summary = OptionalText.using(label=L_("Summary")).with_properties(placeholder=L_("One-line summary of the item"))
    name = Names
    tags = Tags


class BaseModifyForm(BaseChangeForm):
    """
    This class is abstract and only defines two factory methods; see
    Item._ModifyForm for the implementation.
    """
    @classmethod
    def from_item(cls, item):
        """
        Construct an instance from :item.

        This class method is not supposed to be overriden; subclasses should
        overrride the _load method instead.
        """
        form = cls.from_defaults()
        form._load(item)
        return form

    @classmethod
    def from_request(cls, request):
        """
        Construct an instance from :request.

        Since the mapping from HTTP form (unlike from an Item instance) to
        Flatland Form is straightforward, there should be rarely any need to
        override this class method.
        """
        form = cls.from_flat(request.form.items() + request.files.items())
        return form


UNKNOWN_ITEM_GROUP = "Unknown Items"


def _build_contenttype_query(groups):
    """
    Build a Whoosh query from a list of contenttype groups.
    """
    queries = []
    for g in groups:
        for e in content_registry.groups[g]:
            ct_unicode = unicode(e.content_type)
            queries.append(Term(CONTENTTYPE, ct_unicode))
            queries.append(Prefix(CONTENTTYPE, ct_unicode + u';'))
    return Or(queries)


IndexEntry = namedtuple('IndexEntry', 'relname fullname meta')

MixedIndexEntry = namedtuple('MixedIndexEntry', 'relname fullname meta hassubitems')


def get_itemtype_specific_tags(itemtype):
    """
    Returns the tags of a specific itemtype
    """
    with flaskg.storage.indexer.ix[LATEST_REVS].searcher() as searcher:
        items = searcher.search(Term(ITEMTYPE, itemtype), limit=None)
        tags = set()
        for item in items:
            tags.update(item[TAGS])
        return tags


class NameNotUniqueError(ValueError):
    """
    An item with the same name exists.
    """


class MissingParentError(ValueError):
    """
    Cannot create a subitem before creating the parent item.
    """


class FieldNotUniqueError(ValueError):
    """
    The Field is not a UFIELD(unique Field).
    Non unique fields can refer to more than one item.
    """


class Item(object):
    """ Highlevel (not storage) Item, wraps around a storage Revision"""
    # placeholder values for registry entry properties
    itemtype = ''
    display_name = u''
    description = u''
    shown = True
    order = 0

    @classmethod
    def _factory(cls, *args, **kw):
        return cls(*args, **kw)

    @classmethod
    def create(cls, name=u'', itemtype=None, contenttype=None, rev_id=CURRENT, item=None):
        """
        Create a highlevel Item by looking up :name or directly wrapping
        :item and extract the Revision designated by :rev_id revision.

        The highlevel Item is created by creating an instance of Content
        subclass according to the item's contenttype metadata entry; The
        :contenttype argument can be used to override contenttype. It is used
        only when handling +convert (when deciding the contenttype of target
        item), +modify (when creating a new item whose contenttype is not yet
        decided), +diff and +diffraw (to coerce the Content to a common
        super-contenttype of both revisions).

        After that the Content instance, an instance of Item subclass is
        created according to the item's itemtype metadata entry, and the
        previously created Content instance is assigned to its content
        property.
        """
        fqname = split_fqname(name)
        if fqname.field not in UFIELDS:  # Need a unique key to extract stored item.
            raise FieldNotUniqueError("field {0} is not in UFIELDS".format(fqname.field))

        rev = get_storage_revision(fqname, itemtype, contenttype, rev_id, item)
        contenttype = rev.meta.get(CONTENTTYPE) or contenttype
        logging.debug("Item {0!r}, got contenttype {1!r} from revision meta".format(name, contenttype))
        # logging.debug("Item %r, rev meta dict: %r" % (name, dict(rev.meta)))

        # XXX Cannot pass item=item to Content.__init__ via
        # content_registry.get yet, have to patch it later.
        content = Content.create(contenttype)

        itemtype = rev.meta.get(ITEMTYPE) or itemtype or ITEMTYPE_DEFAULT
        logging.debug("Item {0!r}, got itemtype {1!r} from revision meta".format(name, itemtype))

        item = item_registry.get(itemtype, fqname, rev=rev, content=content)
        logging.debug("Item class {0!r} handles {1!r}".format(item.__class__, itemtype))

        content.item = item
        return item

    def __init__(self, fqname, rev=None, content=None):
        self.fqname = fqname
        self.rev = rev
        self.content = content

    def get_meta(self):
        return self.rev.meta
    meta = property(fget=get_meta)

    @property
    def name(self):
        """
        returns the first name from the list of names.
        """
        try:
            return self.names[0]
        except IndexError:
            return u''

    @property
    def names(self):
        """
        returns a list of 0..n names of the item
        If we are dealing with a specific name (e.g field being NAME_EXACT),
        move it to position 0 of the list, so the upper layer can use names[0]
        if they want that particular name and names for the whole list.
        TODO make the entire code to be able to use names instead of name
        """
        names = self.meta.get(NAME, [])
        if self.fqname.field == NAME_EXACT:
            try:
                names.remove(self.fqname.value)
            except ValueError:
                pass
            names.insert(0, self.fqname.value)
        return names

    # XXX Backward compatibility, remove soon
    @property
    def contenttype(self):
        return self.content.contenttype if self.content else None

    def _meta_info(self):
        return self.meta_to_dict(self.meta, use_filter=False)

    def meta_filter(self, meta):
        """ kill metadata entries that we set automatically when saving """
        kill_keys = [  # shall not get copied from old rev to new rev
            # As we have a special field for NAME we don't want NAME to appear in JSON meta.
            NAME, NAME_OLD,
            # are automatically implanted when saving
            REVID, DATAID,
            HASH_ALGORITHM,
            SIZE,
            COMMENT,
            MTIME,
            ACTION,
            ADDRESS, HOSTNAME, USERID,
        ]
        for key in kill_keys:
            meta.pop(key, None)
        return meta

    def meta_to_dict(self, meta, use_filter=True):
        """ convert meta data from storage object to python dict """
        meta = dict(meta)
        if use_filter:
            meta = self.meta_filter(meta)
        return meta

    def meta_text_to_dict(self, text):
        """ convert meta data from a text fragment to a dict """
        meta = json.loads(text)
        return self.meta_filter(meta)

    def meta_dict_to_text(self, meta, use_filter=True):
        """ convert meta data from a dict to a text fragment """
        meta = self.meta_to_dict(meta, use_filter)
        return json.dumps(meta, sort_keys=True, indent=2, ensure_ascii=False)

    def prepare_meta_for_modify(self, meta):
        """
        transform the meta dict of the current revision into a meta dict
        that can be used for saving next revision (after "modify").
        """
        meta = dict(meta)
        revid = meta.pop(REVID, None)
        if revid is not None:
            meta[PARENTID] = revid
        return meta

    def _rename(self, name, comment, action, delete=False):
        self._save(self.meta, self.content.data, name=name, action=action, comment=comment, delete=delete)
        old_prefix = self.subitem_prefixes[0]
        old_prefixlen = len(old_prefix)
        if not delete:
            new_prefix = name + '/'
        for child in self.get_subitem_revs():
            for child_oldname in child.meta[NAME]:
                if child_oldname.startswith(old_prefix):
                    if delete:
                        child_newname = None
                    else:  # rename
                        child_newname = new_prefix + child_oldname[old_prefixlen:]
                    old_fqname = CompositeName(self.fqname.namespace, self.fqname.field, child_oldname)
                    item = Item.create(old_fqname.fullname)
                    item._save(item.meta, item.content.data,
                               name=child_newname, action=action, comment=comment, delete=delete)

    def rename(self, name, comment=u''):
        """
        rename this item to item <name> (replace current name by another name in the NAME list)
        """
        fqname = CompositeName(self.fqname.namespace, self.fqname.field, name)
        if flaskg.storage.get_item(**fqname.query):
            raise NameNotUniqueError(L_("An item named %s already exists in the namespace %s." % (name, fqname.namespace)))
        # if this is a subitem, verify all parent items exist
        _verify_parents(self, name, self.fqname.namespace, old_name=self.fqname.value)
        return self._rename(name, comment, action=ACTION_RENAME)

    def delete(self, comment=u''):
        """
        delete this item (remove current name from NAME list)
        """
        item_modified.send(app, fqname=self.fqname, action=ACTION_TRASH, meta=self.meta,
                           content=self.rev.data, comment=comment)
        ret = self._rename(None, comment, action=ACTION_TRASH, delete=True)
        if [self.name] == self.names:
            flash(L_('The item "%(name)s" was deleted.', name=self.name), 'info')
        else:
            # the item has several names, we deleted only one name
            name_list = [x for x in self.names if not x == self.name]
            msg = L_('The item name "%(name)s" was deleted, the item persists with alias names: "%(name_list)s"', name=self.name, name_list=name_list)
            flash(msg, 'info')
        return ret

    def revert(self, comment=u''):
        return self._save(self.meta, self.content.data, action=ACTION_REVERT, comment=comment)

    def destroy(self, comment=u'', destroy_item=False, subitem_names=[]):
        # called from destroy UI/POST
        action = DESTROY_ALL if destroy_item else DESTROY_REV
        item_modified.send(app, fqname=self.fqname, action=action, meta=self.meta,
                           content=self.rev.data, comment=comment)
        close_file(self.rev.data)
        if destroy_item:
            # destroy complete item with all revisions, metadata, etc.
            self.rev.item.destroy_all_revisions()
            # destroy all subitems
            for subitem_name in subitem_names:
                item = Item.create(subitem_name, rev_id=CURRENT)
                close_file(item.rev.data)
                item.rev.item.destroy_all_revisions()
            flash(L_('The item "%(name)s" was destroyed.', name=self.name), 'info')
        else:
            # just destroy this revision
            self.rev.item.destroy_revision(self.rev.revid)
            flash(L_('Rev Number %(rev_number)s of the item "%(name)s" was destroyed.', rev_number=self.meta['rev_number'], name=self.name), 'info')

    def modify(self, meta, data, comment=u'', contenttype_guessed=None, **update_meta):
        meta = dict(meta)  # we may get a read-only dict-like, copy it
        # get rid of None values
        update_meta = {key: value for key, value in update_meta.items() if value is not None}
        meta.update(update_meta)
        return self._save(meta, data, contenttype_guessed=contenttype_guessed, comment=comment)

    class _ModifyForm(BaseModifyForm):
        """
        ModifyForm (the form used on +modify view), sans the content part.
        Combined dynamically with the ModifyForm of the Content subclass in
        Contentful.ModifyForm.

        Subclasses of Contentful should generally override this instead of
        ModifyForm.
        """
        meta_form = BaseMetaForm
        extra_meta_text = JSON.using(label=L_("Extra MetaData (JSON)")).with_properties(rows=ROWS_META, cols=COLS)
        meta_template = 'modify_meta.html'

        def _load(self, item):
            """
            Load metadata and data from :item into :self. Used by
            BaseModifyForm.from_item.
            """
            meta = item.prepare_meta_for_modify(item.meta)
            # Default value of `policy` argument of Flatland.Dict.set's is
            # 'strict', which causes KeyError to be thrown when meta contains
            # meta keys that are not present in self['meta_form']. Setting
            # policy to 'duck' suppresses this behavior.
            if 'acl' not in meta:
                meta['acl'] = "None"

            self['meta_form'].set(meta, policy='duck')
            for k in self['meta_form'].field_schema_mapping.keys() + IMMUTABLE_KEYS:
                meta.pop(k, None)
            self['extra_meta_text'].set(item.meta_dict_to_text(meta))
            self['content_form']._load(item.content)

        def _dump(self, item):
            """
            Dump useful data out of :self. :item contains the old item and
            should not be the primary data source; but it can be useful in case
            the data in :self is not sufficient.

            :returns: a tuple (meta, data, contenttype_guessed, comment),
                      suitable as arguments of the same names to pass to
                      item.modify
            """
            # Since the metadata form for tickets is an incomplete one, we load the
            # original meta and update it with those from the metadata editor
            # e.g. we get PARENTID in here
            meta = item.meta_filter(item.prepare_meta_for_modify(item.meta))
            meta.update(self['meta_form'].value)
            meta.update(item.meta_text_to_dict(self['extra_meta_text'].value))
            data, contenttype_guessed = self['content_form']._dump(item.content)
            comment = self['comment'].value
            return meta, data, contenttype_guessed, comment

    def do_modify(self):
        """
        Handle +modify requests, both GET and POST.

        This method should be overridden in subclasses, providing polymorphic
        behavior for the +modify view.
        """
        raise NotImplementedError

    def _save(self, meta, data=None, name=None, action=ACTION_SAVE, contenttype_guessed=None, comment=None,
              overwrite=False, delete=False):
        backend = flaskg.storage
        storage_item = backend.get_item(**self.fqname.query)
        try:
            currentrev = storage_item.get_revision(CURRENT)
            rev_id = currentrev.revid
            contenttype_current = currentrev.meta.get(CONTENTTYPE)
        except KeyError:  # XXX was: NoSuchRevisionError:
            currentrev = None
            rev_id = None
            contenttype_current = None

        meta = dict(meta)  # we may get a read-only dict-like, copy it

        if 'acl' in meta:
            # we treat this as nothing specified, so fallback to default
            if meta['acl'] == 'None':
                meta.pop('acl')
            # this is treated as a rule which matches nothing
            elif meta['acl'] == 'Empty':
                meta['acl'] = ''

        # we store the previous (if different) and current item name into revision metadata
        # this is useful for rename history and backends that use item uids internally
        if self.fqname.field == NAME_EXACT:
            if name is None:
                name = self.fqname.value
            oldname = meta.get(NAME)
            if oldname:
                if not isinstance(oldname, list):
                    oldname = [oldname]
                if delete or name not in oldname:  # this is a delete or rename
                    try:
                        oldname.remove(self.name)
                    except ValueError:
                        pass
                    if not delete:
                        oldname.append(name)
                    meta[NAME] = oldname
            elif not meta.get(ITEMID):
                meta[NAME] = [name]
        elif self.fqname.field == ITEMID and delete:
            # delete by itemid will display deleted item with flash message
            meta[NAME] = []

        if meta.get(NAMESPACE) is None:
            meta[NAMESPACE] = self.fqname.namespace

        if comment is not None:
            meta[COMMENT] = unicode(comment)

        if currentrev:
            current_names = currentrev.meta.get(NAME, [])
            new_names = meta.get(NAME, [])
            deleted_names = set(current_names) - set(new_names)
            if deleted_names:  # some names have been deleted.
                meta[NAME_OLD] = current_names
                # if no names left, then set the trash but not if the item is a ticket (tickets get closed, not deleted)
                if not new_names and (ITEMTYPE not in meta or not meta[ITEMTYPE] == ITEMTYPE_TICKET):
                    meta[TRASH] = True
            meta[REV_NUMBER] = currentrev.meta.get(REV_NUMBER, 0) + 1
        else:
            meta[REV_NUMBER] = 1

        if not overwrite and REVID in meta:
            # we usually want to create a new revision, thus we update parentid and remove the existing REVID
            meta[PARENTID] = currentrev.meta[REVID] if currentrev else meta[REVID]
            del meta[REVID]

        if data is None:
            if currentrev is not None:
                # we don't have (new) data, just copy the old one.
                # a valid usecase of this is to just edit metadata.
                data = currentrev.data
            else:
                data = ''

        data = self.handle_variables(data, meta)

        if isinstance(data, unicode):
            data = data.encode(CHARSET)  # XXX wrong! if contenttype gives a coding, we MUST use THAT.

        if isinstance(data, bytes):
            data = BytesIO(data)
        newrev = storage_item.store_revision(meta, data, overwrite=overwrite,
                                             action=unicode(action),
                                             contenttype_current=contenttype_current,
                                             contenttype_guessed=contenttype_guessed,
                                             return_rev=True,
                                             )
        item_modified.send(app, fqname=newrev.fqname, action=action)
        return newrev.revid, newrev.meta[SIZE]

    def handle_variables(self, data, meta):
        """ Expand @VARIABLE@ in data, where variable is SIG, DATE, etc

        TODO: add a means for wiki admins and users to add custom variables.

        @param data: text of wikipage
        @param meta: meta of wikipage
        @rtype: string
        @return: new text of wikipage, variables replaced
        """
        logging.debug("handle_variable data: %r" % data)
        if self.contenttype not in CONTENTTYPE_VARIABLES:
            return data
        if '@' not in data:
            return data
        if not request.path.startswith('/+modify'):
            return data
        if TEMPLATE in meta['tags']:
            return data

        item_name = request.path.split('/', 2)[-1]
        signature = flaskg.user.name0 if flaskg.user.valid else request.remote_addr

        variables = {
            'PAGE': item_name,
            'ITEM': item_name,
            'TIMESTAMP': strftime("%Y-%m-%d %H:%M:%S %Z"),
            'TIME': "<<DateTime(%s)>>" % time(),
            'DATE': "<<Date(%s)>>" % time(),
            'ME': flaskg.user.name0,
            'USERNAME': signature,
            'USER': "-- %s" % signature,
            'SIG': "-- %s <<DateTime(%s)>>" % (signature, time()),
        }

        email = flaskg.user.profile._meta.get('email', None)
        if email:
            obfuscated_email_address = encodeSpamSafeEmail(email)
            variables['MAILTO'] = "<<MailTo({0})>>".format(obfuscated_email_address)
            variables['EMAIL'] = "<<MailTo({0})>>".format(email)
        else:
            # penality for not being logged in is a mangled variable, else next user to save item may accidently reveal his email address
            variables['MAILTO'] = "@ EMAIl@"
            variables['EMAIL'] = "@ MAILTO@"

        for name in variables:
            try:
                data = data.replace(u'@{0}@'.format(name), variables[name])
            except UnicodeError:
                logging.warning("handle_variables: UnicodeError! name: %r value: %r" % (name, variables[name]))
        return data

    @property
    def subitem_prefixes(self):
        """
        Return the possible prefixes for subitems.
        """
        names = self.names[0:1] if self.fqname.field == NAME_EXACT else self.names
        return [name + u'/' if name else u'' for name in names]

    def get_prefix_match(self, name, prefixes):
        """
        returns the prefix match found.
        """
        for prefix in prefixes:
            if name.startswith(prefix):
                return prefix

    def get_subitem_revs(self):
        """
        Create a list of subitems of this item.

        Subitems are in the form of storage Revisions.
        """
        query = And([Term(WIKINAME, app.cfg.interwikiname), Term(NAMESPACE, self.fqname.namespace)])
        # trick: an item of empty name can be considered as "virtual root item"
        # that has all wiki items as sub items
        if self.names:
            query = And([query, Or([Prefix(NAME_EXACT, prefix) for prefix in self.subitem_prefixes])])
        revs = flaskg.storage.search(query, sortedby=NAME_EXACT, limit=None)
        return revs

    def make_flat_index(self, subitems, isglobalindex=False):
        """
        Create two IndexEntry lists - ``dirs`` and ``files`` - from a list of
        subitems.

        Direct subitems are added to the ``files`` list.

        For indirect subitems, its ancestor which is a direct subitem is added
        to the ``dirs`` list. Supposing current index root is 'foo' and when
        'foo/bar/la' is encountered, 'foo/bar' is added to ``dirs``.

        The direct subitem need not exist.

        When both a subitem itself and some of its subitems are in the subitems
        list, it appears in both ``files`` and ``dirs``.

        :param isglobalindex: True if the query is for global indexes.
        """
        prefixes = [u''] if isglobalindex else self.subitem_prefixes
        # IndexEntry instances of "file" subitems
        files = []
        # IndexEntry instances of "directory" subitems
        dirs = []
        added_dir_relnames = set()
        for rev in subitems:
            fullnames = rev.meta[NAME]
            for fullname in fullnames:
                prefix = self.get_prefix_match(fullname, prefixes)
                if prefix is not None:
                    fullname_fqname = CompositeName(rev.meta[NAMESPACE], NAME_EXACT, fullname)
                    relname = fullname[len(prefix):]
                    if '/' in relname:
                        # Find the *direct* subitem that is the ancestor of current
                        # (indirect) subitem. e.g. suppose when the index root is
                        # 'foo', and current item (`rev`) is 'foo/bar/lorem/ipsum',
                        # 'foo/bar' will be found.
                        direct_relname = relname.partition('/')[0]
                        direct_relname_fqname = CompositeName(rev.meta[NAMESPACE], NAME_EXACT, direct_relname)
                        if direct_relname_fqname not in added_dir_relnames:
                            added_dir_relnames.add(direct_relname_fqname)
                            direct_fullname = prefix + direct_relname
                            direct_fullname_fqname = CompositeName(rev.meta[NAMESPACE], NAME_EXACT, direct_fullname)
                            fqname = CompositeName(rev.meta[NAMESPACE], NAME_EXACT, direct_fullname)
                            try:
                                direct_rev = get_storage_revision(fqname)
                            except AccessDenied:
                                continue  # user has permission to see parent, but not this subitem
                            dirs.append(IndexEntry(direct_relname, direct_fullname_fqname, direct_rev.meta))
                    else:
                        files.append(IndexEntry(relname, fullname_fqname, rev.meta))
        files.sort()  # files with multiple names are not in sequence
        return dirs, files

    def build_index_query(self, startswith=None, selected_groups=None, isglobalindex=False):
        """
        Builds a query string that can be passed to the storage search engine.

        Input:
           startswith: build query to select items that begin with the specified substring,
                       or None if unconstrained.
           selected_groups: ???
           isglobalindex: ???

        Output:
          Returns a whoosh.query.Prefix object for the input parameters
        """
        prefix = u'' if isglobalindex else self.subitem_prefixes[0]
        if startswith:
            query = Prefix(NAME_EXACT, prefix + startswith) | Prefix(NAME_EXACT, prefix + startswith.swapcase())
        else:
            query = Prefix(NAME_EXACT, prefix)

        if selected_groups:
            selected_groups = set(selected_groups)
            has_unknown = UNKNOWN_ITEM_GROUP in selected_groups
            if has_unknown:
                selected_groups.remove(UNKNOWN_ITEM_GROUP)
            ct_query = _build_contenttype_query(selected_groups)
            if has_unknown:
                ct_query |= Not(_build_contenttype_query(content_registry.groups))
            query &= ct_query

        return query

    def get_index(self, startswith=None, selected_groups=None):
        """
        Get index enties for descendents of the matching items

        Input:
           startswith: select items whose names begin with the specifiedi
                       substring, or None if unconstrained
           selected_groups: ???

        Output:
           Returns two IndexEntry (i.e., collections.namedtuple) arrays:
             - one for "files" (direct descendents that do not contain any descendents)
             - one for "dirs" (direct descendents that also contain descendents)
        """
        fqname = self.fqname
        isglobalindex = not fqname.value or fqname.value == NAMESPACE_ALL
        query = Term(WIKINAME, app.cfg.interwikiname) & self.build_index_query(startswith, selected_groups, isglobalindex)
        if not fqname.value.startswith(NAMESPACE_ALL + '/') and fqname.value != NAMESPACE_ALL:
            query = Term(NAMESPACE, fqname.namespace) & query
        revs = flaskg.storage.search(query, sortedby=NAME_EXACT, limit=None)
        return self.make_flat_index(revs, isglobalindex)

    def get_mixed_index(self):
        dirs, files = self.make_flat_index(self.get_subitem_revs())
        dirs_dict = dict([(e.fullname, MixedIndexEntry(*e, hassubitems=True)) for e in dirs])
        index_dict = dict([(e.fullname, MixedIndexEntry(*e, hassubitems=False)) for e in files])
        index_dict.update(dirs_dict)
        return sorted(index_dict.values())


class Contentful(Item):
    """
    Base class for Item subclasses that have content.
    """
    @property
    def ModifyForm(self):
        class C(self._ModifyForm):
            content_form = self.content.ModifyForm
        C.__name__ = 'ModifyForm'
        return C


@register
class Default(Contentful):
    """
    A "conventional" wiki item.
    """
    itemtype = ITEMTYPE_DEFAULT
    display_name = L_('Default')
    description = L_('Wiki item')
    order = -10

    def _do_modify_show_templates(self):
        # call this if the item is still empty
        rev_ids = []
        item_templates = self.content.get_templates(self.contenttype)
        if not item_templates:
            return redirect(url_for('frontend.modify_item', item_name=self.fqname, itemtype=self.itemtype,
                                    contenttype=self.contenttype, template=''))
        return render_template('modify_select_template.html',
                               item=self,
                               item_name=self.name,
                               fqname=self.fqname,
                               itemtype=self.itemtype,
                               rev=self.rev,
                               contenttype=self.contenttype,
                               templates=item_templates,
                               first_rev_id=rev_ids and rev_ids[0],
                               last_rev_id=rev_ids and rev_ids[-1],
                               meta_rendered='',
                               data_rendered='',
                               )

    def do_show(self, revid):
        """
        Display an item. If this is not the current revision, page content will be
        prefaced with links to the next-rev and prior-rev.
        """
        rev_navigation_ids_dates = rev_navigation.prior_next_revs(revid, self.fqname)
        # create extra meta tags for use by web crawlers
        html_head_meta = {}
        if 'tags' in self.meta and self.meta['tags']:
            html_head_meta['keywords'] = ', '.join(self.meta['tags'])
        if 'summary' in self.meta and self.meta['summary']:
            html_head_meta['description'] = self.meta['summary']
        return render_template('show.html',
                               item=self,
                               item_name=self.name,
                               fqname=self.fqname,
                               rev=self.rev,
                               contenttype=self.contenttype,
                               rev_navigation_ids_dates=rev_navigation_ids_dates,
                               data_rendered=Markup(self.content._render_data()),
                               html_head_meta=html_head_meta,
                              )

    def doc_link(self, filename, link_text):
        """create a link to serve local doc files as help for wiki editors"""
        filename = url_for('serve.files', name='docs', filename=filename)
        return u'<a href="%s">%s</a>' % (filename, link_text)

    def meta_changed(self, meta):
        """
        Return true if user changed any of the following meta data:
            comment, ACL, summary, tags, names
        """
        if request.values.get(COMMENT):
            return True
        if request.values.get('meta_form_acl') != meta.get('acl', 'None'):
            return True
        if request.values.get('meta_form_summary') != meta.get('summary', None):
            return True
        new_tags = request.values.get('meta_form_tags').replace(" ", "").split(',')
        if new_tags == [u""]:
            new_tags = []
        if new_tags != meta.get('tags', None):
            return True
        new_name = request.values.get('meta_form_name').replace(" ", "").split(',')
        if new_name != meta.get('name', None):
            return True
        return False

    def do_modify(self):
        if isinstance(self.content, NonExistentContent) and not flaskg.user.may.create(self.name):
            abort(403, description=' ' + _('You do not have permission to create the item named "{name}".'.format(name=self.name)))

        method = request.method
        if method in ['GET', 'HEAD']:
            if isinstance(self.content, NonExistentContent):
                return render_template('modify_select_contenttype.html',
                                       fqname=self.fqname,
                                       item_name=self.name,
                                       itemtype=self.itemtype,
                                       group_names=content_registry.group_names,
                                       groups=content_registry.groups,
                                      )

        item = self
        flaskg.edit_utils = edit_utils = Edit_Utils(self)

        # these will be updated if user has clicked Preview
        preview_diffs = ''
        preview_rendered = ''

        if request.values.get('cancel'):
            edit_utils.delete_draft()
            if app.cfg.edit_locking_policy == LOCK:
                edit_utils.unlock_item(cancel=True)
            edit_utils.cursor.close()
            return redirect(url_for_item(**self.fqname.split))

        if app.cfg.edit_locking_policy == LOCK:
            locked, msg = edit_utils.lock_item()
            if msg:
                flash(msg, "info")
            if not locked == LOCKED:
                # edit locking policy is True and someone else has file locked
                edit_utils.cursor.close()
                return redirect(url_for_item(self.name))
        elif method in ['GET', 'HEAD']:
            # if there is not a draft row, create one to aid in conflict detection
            edit_utils.put_draft(None, overwrite=False)

        if isinstance(self.rev, DummyRev):
            template_name = request.values.get(TEMPLATE)
            if template_name is None:
                edit_utils.cursor.close()
                return self._do_modify_show_templates()
            # template_name == u'' when user chooses "create item from scratch"
            elif template_name:
                item = Item.create(template_name)
                form = self.ModifyForm.from_item(item)
                # replace template name with new item name and remove TEMPLATE tag
                form['meta_form']['name'] = self.names[0]
                form['meta_form']['tags'].remove(TEMPLATE)
            else:
                form = self.ModifyForm.from_item(item)
        else:
            form = self.ModifyForm.from_item(item)

        if method == 'POST':
            # XXX workaround for *Draw items
            if isinstance(self.content, Draw):
                try:
                    self.content.handle_post()
                except AccessDenied:
                    edit_utils.cursor.close()
                    abort(403)
                else:
                    # *Draw Applets POSTs more than once, redirecting would
                    # break them
                    edit_utils.cursor.close()
                    return "OK"

            form = self.ModifyForm.from_request(request)
            meta, data, contenttype_guessed, comment = form._dump(self)
            if contenttype_guessed:
                m = re.search('charset=(.+?)($|;)', contenttype_guessed)
                if m:
                    data = unicode(data, m.group(1))
            state = dict(fqname=self.fqname, itemid=meta.get(ITEMID), meta=meta)
            if form.validate(state):
                if request.values.get('preview'):
                    # user has clicked Preview button, create diff and rendered item
                    edit_utils.put_draft(data)
                    old_item = Item.create(self.fqname.fullname, rev_id=CURRENT, contenttype=self.contenttype)
                    old_text = old_item.content.data
                    old_text = Text(self.contenttype, item=item).data_storage_to_internal(old_text)
                    preview_diffs = [(d[0], Markup(d[1]), d[2], Markup(d[3])) for d in html_diff(old_text, data)]
                    preview_rendered = item.content._render_data(preview=data)
                else:
                    # user clicked OK/Save button, check for conflicts,
                    draft, draft_data = edit_utils.get_draft()
                    if draft:
                        u_name, i_id, i_name, rev_number, save_time, rev_id = draft
                        if not rev_id == 'new-item':
                            original_item = Item.create(self.name, rev_id=rev_id, contenttype=self.contenttype)
                            original_text = original_item.content.data
                        else:
                            original_text = u''
                        if original_text == data and not self.meta_changed(item.meta):
                            flash(_("Nothing changed, nothing saved."), "info")
                            edit_utils.delete_draft()
                            if app.cfg.edit_locking_policy == LOCK:
                                edit_utils.unlock_item(cancel=True)
                            edit_utils.cursor.close()
                            return redirect(url_for_item(**self.fqname.split))

                        if rev_number < self.meta.get('rev_number', 0):
                            # we have conflict - someone else has saved item, create and save 3-way diff, give user error message to fix it
                            saved_item = Item.create(self.name, rev_id=CURRENT, contenttype=self.contenttype)
                            saved_text = saved_item.content.data
                            data3 = diff3.text_merge(original_text, saved_text, data)
                            data = data3
                            comment = _("CONFLICT ") + comment or ''
                            flash(_("An edit conflict has occurred, edit this item again to resolve conflicts."), "error")

                    # save the new revision, unlock, delete draft
                    contenttype_qs = request.values.get('contenttype')
                    try:
                        self.modify(meta, data, comment, contenttype_guessed, **{CONTENTTYPE: contenttype_qs})
                    except AccessDenied:
                        edit_utils.cursor.close()
                        abort(403)
                    else:
                        close_file(self.rev.data)
                        edit_utils.delete_draft()
                        if app.cfg.edit_locking_policy == LOCK:
                            locked_msg = edit_utils.unlock_item()
                            if locked_msg:
                                logging.error("Item saved but locked by someone else: {0!r}".format(self.fqname))
                                flash(locked_msg, "info")
                        edit_utils.cursor.close()
                        return redirect(url_for_item(**self.fqname.split))

        # prepare to show modify.html form, request is either +Modify GET or Preview (Save and Cancel processing complete)
        help = CONTENTTYPES_HELP_DOCS[self.contenttype]
        if isinstance(help, tuple):
            help = self.doc_link(*help)
        if flaskg.user.valid and EDIT_ROWS in flaskg.user.profile._meta:
            edit_rows = str(flaskg.user.profile._meta[EDIT_ROWS])
        else:
            edit_rows = str(flaskg.user.profile._defaults[EDIT_ROWS])
        close_file(self.rev.data)
        draft_data = None
        if not request.values.get('preview'):
            # request is +Modify GET, check for abandoned draft
            draft, draft_data = edit_utils.get_draft()
            if draft:
                u_name, i_id, i_name, rev_number, save_time, rev_id = draft
                if save_time:
                    # if revno = current: you may recover a saved draft by clicking load draft button
                    # if revno < current: a old draft is available, loading it will create a conflict that  must be merged manually
                    interval, number = show_time.duration(time() - save_time)
                    if self.rev.meta.get(REV_NUMBER, 0) == rev_number:
                        flash(L_("You may recover your draft saved %(number)s %(interval)s ago by clicking the 'Load Draft' button.", number=number, interval=interval, ), 'info')
                    else:
                        flash(L_("Your draft saved %(number)s %(interval)s ago is outdated, click 'Cancel' to discard or 'Load Draft', then 'Save' to merge conflicting updates.", number=number, interval=interval, ), 'error')

        if app.cfg.edit_locking_policy == LOCK:
            # we pass lock_duration so javascript can show alert before timer expires
            lock_duration = app.cfg.edit_lock_time * 60
        else:
            lock_duration = None
        # if request is +modify GET we show modify form, else if POST Preview we show modify form + diff + rendered item
        edit_utils.cursor.close()
        return render_template('modify.html',
                               fqname=self.fqname,
                               item_name=self.name,
                               item=self,
                               rows_meta=str(ROWS_META),
                               cols=str(COLS),
                               form=form,
                               search_form=None,
                               help=help,
                               preview_diffs=preview_diffs,
                               preview_rendered=preview_rendered,
                               edit_rows=edit_rows,
                               draft_data=draft_data,
                               lock_duration=lock_duration,
                              )


@register
class Userprofile(Item):
    """
    Currently userprofile is implemented as a contenttype. This is a stub of an
    itemtype implementation of userprofile.
    """
    itemtype = ITEMTYPE_USERPROFILE
    display_name = L_('User profile')
    description = L_('User profile item (not implemented yet!)')


@register
class NonExistent(Item):
    """
    A dummy Item for nonexistent items (when modifying, a nonexistent item with
    undetermined itemtype)
    """
    itemtype = ITEMTYPE_NONEXISTENT
    shown = False

    def _convert(self, doc):
        abort(404)

    def do_show(self, revid):
        # First, check if the current user has the required privileges
        if flaskg.user.may.create(self.name):
            content = self._select_itemtype()
        else:
            content = render_template('show_nonexistent.html',
                                      item_name=self.name,
                                      fqname=self.fqname,
                                     )
        return Response(content, 404)

    def do_modify(self):
        # First, check if the current user has the required privileges
        if not flaskg.user.may.create(self.name):
            abort(403)
        return self._select_itemtype()

    def _select_itemtype(self):
        """
        TODO: Here we bypass the code that allows a user to select an itemtype just before
        creating a new item:

            Default - Wiki item
            User profile - User profile item (not implemented yet!)
            Blog - Blog item
            Blog entry - Blog entry item
            Ticket - Ticket item

        Blogs and Tickets are broken, why User Profile is here is an undocumented mystery (it is
        probably no longer required).

        If you want to work on tickets or blogs, create a new branch and revert the change
        made on or about 2017-07-04:
        """
        # if this is a subitem, verify all parent items exist
        try:
            _verify_parents(self, self.name, self.meta[NAMESPACE])
        except MissingParentError as e:
            flash(str(e), "error")
            form = CreateItemForm().from_defaults()
            form['target'] = self.fqname.fullname
            return render_template('create_new_item.html',
                                   fqname=self.fqname,
                                   form=form,
            )

        return render_template('modify_select_contenttype.html',
                               fqname=self.fqname,
                               item_name=self.name,
                               itemtype='default',  # create a normal wiki item
                               group_names=content_registry.group_names,
                               groups=content_registry.groups,
                              )
        # dead code, see above
        return render_template('modify_select_itemtype.html',
                               item=self,
                               item_name=self.name,
                               fqname=self.fqname,
                               itemtypes=item_registry.shown_entries,
                              )

    def rename(self, name, comment=u''):
        # pointless for non-existing items
        pass

    def delete(self, comment=u''):
        # pointless for non-existing items
        pass

    def revert(self, comment=u''):
        # pointless for non-existing items
        pass

    def destroy(self, comment=u'', destroy_item=False):
        # pointless for non-existing items
        pass


load_package_modules(__name__, __path__)
