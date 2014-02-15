# Copyright (c) 2013 Shotgun Software Inc.
# 
# CONFIDENTIAL AND PROPRIETARY
# 
# This work is provided "AS IS" and subject to the Shotgun Pipeline Toolkit 
# Source Code License included in this distribution package. See LICENSE.
# By accessing, using, copying or modifying this work you indicate your 
# agreement to the Shotgun Pipeline Toolkit Source Code License. All rights 
# not expressly granted therein are reserved by Shotgun Software Inc.

import tank
import copy
import os
import hashlib
import datetime
import time
import tempfile
from .overlaywidget import OverlayWidget
from .sgdata import ShotgunAsyncDataRetriever

from tank.platform.qt import QtCore, QtGui

# just so we can do some basic file validation
FILE_MAGIC_NUMBER = 0xDEADBEEF # so we can validate file format correctness before loading
FILE_VERSION = 10               # if we ever change the file format structure


class ShotgunModel(QtGui.QStandardItemModel):
    """
    A Shotgun model which makes it easy to create Shotgun QT data sources.
    """

    SG_DATA_ROLE = QtCore.Qt.UserRole + 1
    IS_SG_MODEL_ROLE = QtCore.Qt.UserRole + 2
    SG_ASSOCIATED_FIELD_ROLE = QtCore.Qt.UserRole + 3

    def __init__(self, parent, overlay_parent_widget, download_thumbs):
        """
        Constructor. This will create a model which can later be used to load
        and manage Shotgun data.
        
        :param overlay_parent_widget: A QWidget object on top of which any progress
                                      overlays will be rendered.
        :param download_thumbs: Boolean to indicate if this model should attempt 
                                to download and process thumbnails for the downloaded data.
        
        """
        QtGui.QStandardItemModel.__init__(self, parent)
        
        # set up data fetcher
        self.__sg_data_retriever = ShotgunAsyncDataRetriever(self)
        self.__sg_data_retriever.work_completed.connect( self.__on_worker_signal)
        self.__sg_data_retriever.work_failure.connect( self.__on_worker_failure)
        self.__current_work_id = 0
        # and start its thread!
        self.__sg_data_retriever.start()
        
        self.__overlay = OverlayWidget(overlay_parent_widget)
        
        self.__all_tree_items = []
        self.__entity_tree_data = {}
        self.__thumb_map = {}
        
        self.__download_thumbs = download_thumbs
        
        self.__app = tank.platform.current_bundle()
        
    ########################################################################################
    # public methods

    def destroy(self):
        """
        Call this method prior to destroying this object.
        This will ensure all worker threads etc are stopped.
        """
        # first disconnect our worker completely
        self.__sg_data_retriever.work_completed.disconnect( self.__on_worker_signal)
        self.__sg_data_retriever.work_failure.disconnect( self.__on_worker_failure)
        # gracefully stop thread
        self.__sg_data_retriever.stop()
        # finally totally deallocate it just to make GC happy
        self.__sg_data_retriever = None
    
    def item_from_entity(self, entity_type, entity_id):
        """
        Returns a QStandardItem based on entity type and entity id
        Returns none if not found.
        """
        if entity_type != self.__entity_type:
            return None
        if entity_id not in self.__entity_tree_data:
            return None
        return self.__entity_tree_data[entity_id]        
         
    def get_filters(self, item):
        """
        Returns a list of filters representing the current item
        """
        # prime filters with our base query
        filters = copy.deepcopy(self.__filters)
        
        # now walk up the tree and get all fields
        p = item
        while p:
            field_data = p.data(ShotgunModel.SG_ASSOCIATED_FIELD_ROLE) 
            filters.append( [ field_data["name"], "is", field_data["value"] ] )
            p = p.parent()
        return filters  
         
    def get_entity_type(self):
        """
        Returns the Shotgun Entity type associated with this model
        """
        return self.__entity_type
         
    def _reset_all_data(self):
        """
        Deletes all the contents of the model. 
        Very similar to the clear() method, however it
        seems clear does not work properly on pyside so 
        we are avoiding that method.
        """
        # ask async data retriever to clear its queue
        # note that there may still be requests actually running
        # - these are not cancelled
        self.__sg_data_retriever.clear()
        # we are not looking for any data from the async processor
        self.__current_work_id = 0
        # model data in alt format
        self.__entity_tree_data = {}
        # thumbnail download lookup
        self.__thumb_map = {}
        # pyside will crash unless we actively hold a reference
        # to all items that we create.
        self.__all_tree_items = []

        # remove all data in the underyling internal data storage
        # note that we don't use clear() here since that causing
        # crashing on nuke/pyside
        self.invisibleRootItem().removeRows(0,self.rowCount())
        
        

    ########################################################################################
    # protected methods not meant to be subclassed but meant to be called by subclasses
    
    def _load_data(self, entity_type, filters, hierarchy, fields, order):
        """
        Clears the model of any previous data and prepares for operation with 
        a new set of shotgun query data. Nothing is retrieved from Shotgun at this point
        but if cache data is available, this is loaded into the model.
        
        The separation between the _load_data and _refresh_data() which actually calls
        out to Shotgun makes it possible to potentially run the model in offline mode.
        
        :param entity_type: Shotgun entity type to download
        :param filters: List of Shotgun filters. Standard Shotgun syntax.
        :param hierarchy: List of grouping fields. These should be names of Shotgun 
                          fields. If you for example want to create a list of items,
                          the value ["code"] will be suitable. This will generate a data
                          model which is flat and where each item's default name is the
                          Shotgun name field. If you want to generate a tree where assets
                          are broken down by asset type, you could instead specify
                          ["sg_asset_type", "code"]
        :param fields:    Fields to retrieve from Shotgun (in addition to the ones specified
                          in the hierarchy parameter). Standard Shotgun API syntax. If you 
                          specify None for this parameter, Shotgun will not be called when
                          the _refresh_data() method is being executed.
        :param order:     Order clause for the Shotgun data. Standard Shotgun API syntax.
        """
        
        # clear out old data
        self._reset_all_data()
        
        self.__overlay.hide()
        self.__entity_type = entity_type
        self.__filters = filters
        self.__fields = fields
        self.__order = order
        self.__hierarchy = hierarchy
        
        # when we cache the data associated with this model, create
        # the file name based on the md5 hash of the filter and other 
        # parameters that will determine the contents that is loaded into the tree
        # note that we add the shotgun host name to support multiple sites being used
        # on a single machine
        hash_base = "%s_%s_%s_%s_%s_%s" % (self.__app.shotgun.base_url, 
                                           self.__entity_type, 
                                           str(self.__filters), 
                                           str(self.__fields),
                                           str(self.__order),
                                           str(self.__hierarchy))
        m = hashlib.md5()
        m.update(hash_base)
        cache_filename = "tk_sgmodel_%s.sgcache" % m.hexdigest()
        self.__full_cache_path = os.path.join(tempfile.gettempdir(), cache_filename)
        
        self.__app.log_debug("-----------------------------------------------------")
        self.__app.log_debug("LOAD DATA + Model reset for %s" % self)
        self.__app.log_debug("Entity type: %s" % self.__entity_type)
        self.__app.log_debug("Cache path: %s" % self.__full_cache_path)
        self.__app.log_debug("Filters: %s" % self.__filters)
        self.__app.log_debug("Hierarchy: %s" % self.__hierarchy)
        self.__app.log_debug("Extra Fields: %s" % self.__fields)
        self.__app.log_debug("Order: %s" % self.__order)
        self.__app.log_debug("-----------------------------------------------------")
        
        self._load_external_data()    
        if os.path.exists(self.__full_cache_path):
            # first see if we need to load in any overlay data from deriving classes
            self.__app.log_debug("Loading cached data %s..." % self.__full_cache_path)
            try:
                
                self.__load_from_disk(self.__full_cache_path)
                self.__app.log_debug("...loading complete!")
            except Exception, e:
                self.__app.log_debug("Couldn't load cache data from disk. Will proceed with "
                                    "full SG load. Error reported: %s" % e)        
    
    def _refresh_data(self):
        """
        Rebuilds the data in the model to ensure it is up to date.
        This call is asynchronous and will return instantly.
        The update will be applied whenever the data from Shotgun is returned.
        """
        
        if len(self.__entity_tree_data) == 0:
            # we are loading an empty tree
            self.__overlay.start_spin()
        
        if self.__filters is None:
            # filters is None indicates that no data is desired.
            # do not issue the sg request but pass straight to the callback
            self.__on_sg_data_arrived([])
        else:
            # get data from shotgun - list/set cast to ensure unique fields
            if self.__download_thumbs:
                fields = list(set(self.__hierarchy + self.__fields + ["image"]))
            else:
                fields = list(set(self.__hierarchy + self.__fields))
            
            self.__current_work_id = self.__sg_data_retriever.execute_find(self.__entity_type, 
                                                                           self.__filters, 
                                                                           fields,
                                                                           self.__order)
    
    
    def _show_overlay_pixmap(self, pixmap):
        """
        Show an overlay status message in the form of a pixmap
        """
        self.__overlay.show_message_pixmap(pixmap)        

    def _show_overlay_info_message(self, msg):
        """
        Show an overlay status message
        """
        self.__overlay.show_message(msg)        
        
    def _show_overlay_error_message(self, msg):
        """
        Show an overlay error message
        """
        self.__overlay.show_error_message(msg)        

    def _request_thumbnail_download(self, item, field, url, entity_type, entity_id):
        """
        Request that a thumbnail is downloaded for an item.
        
        :param item: QStandardItem which belongs to this model
        :param field: Shotgun field where the thumbnail is stored
        :param url: thumbnail url
        :param entity_type: Shotgun entity type
        :param entity_id: Shotgun entity id 
        """
        if url is None:
            # nothing to download. bad input. gracefully ignore this request.
            return
        
        uid = self.__sg_data_retriever.request_thumbnail(url, entity_type, entity_id, field)
        
        # keep tabs of this and call out later
        self.__thumb_map[uid] = {"item": item, "field": field }
        
        
    ########################################################################################
    # methods to be implemented by subclasses    
    
    def _populate_item(self, item, sg_data):
        """
        Whenever an item is constructed, this methods is called. It allows subclasses to intercept
        the construction of a QStandardItem and add additional metadata or make other changes
        that may be useful. Nothing needs to be returned.
        
        :param item: QStandardItem that is about to be added to the model. This has been primed
                     with the standard settings that the ShotgunModel handles.
        :param sg_data: Shotgun data dictionary that was received from Shotgun given the fields
                        and other settings specified in _load_data()
        """
        # default implementation does nothing
        
    def _populate_default_thumbnail(self, item):
        """
        Called whenever an item needs to get a default thumbnail attached to a node.
        When thumbnails are loaded, this will be called first, when an object is
        either created from scratch or when it has been loaded from a cache, then later
        on a call to _populate_thumbnail will follow where the subclassing implementation
        can populate the real image.
        """
        # the default implementation ensures that the icon is cleared
        # this is because when the items are serialized to disk, they seem
        # to store a low res version of the icons, so if the icon isn't cleared
        # it usually shows up as a not-very-looking low res version of the real
        # thumbnail.
        item.setIcon( QtGui.QIcon() )


    def _populate_thumbnail(self, item, field, path):
        """
        Called whenever a thumbnail for an item has arrived on disk. In the case of 
        an already cached thumbnail, this may be called very soon after data has been 
        loaded, in cases when the thumbs are downloaded from Shotgun, it may happen later.
        
        This method will be called only if the model has been instantiated with the 
        download_thumbs flag set to be true. It will be called for items which are
        associated with shotgun entities (in a tree data layout, this is typically 
        leaf nodes).
        
        This method makes it possible to control how the thumbnail is applied and associated
        with the item. The default implementation will simply set the thumbnail to be icon
        of the item, but this can be altered by subclassing this method.
        
        Any thumbnails requested via the _request_thumbnail_download() method will also 
        resurface via this callback method.
        
        :param item: QStandardItem which is associated with the given thumbnail
        :param field: The Shotgun field which the thumbnail is associated with.
        :param path: A path on disk to the thumbnail. This is a file in jpeg format.
        """
        # the default implementation sets the icon
        thumb = QtGui.QPixmap(path)
        item.setIcon(thumb)
        
    def _before_data_processing(self, sg_data_list):
        """
        Called just after data has been retrieved from Shotgun but before any processing
        takes place. This makes it possible for deriving classes to perform summaries, 
        calculations and other manipulations of the data before it is passed on to the model
        class. 
        
        :param sg_data_list: list of shotgun dictionaries, as retunrned by the find() call.
        :returns: should return a list of shotgun dictionaries, on the same form as the input.
        """
        # default implementation is a passthrough
        return sg_data_list

    def _load_external_data(self):
        """
        Called whenever the model needs to be rebuilt from scratch. This is called prior 
        to any shotgun data is added to the model. This makes it possible for deriving classes
        to add custom data to the model in a very flexible fashion. Such data will not be 
        cached by the ShotgunModel framework.
        """
        pass

    ########################################################################################
    # private methods 

    def __on_worker_failure(self, uid, msg):
        """
        Asynchronous callback - the worker thread errored.
        """
        if self.__current_work_id != uid:
            # not our job. ignore
            return
        
        full_msg = "Error retrieving data from Shotgun: %s" % msg
        
        if len(self.__entity_tree_data) == 0:
            # no data laoded yet. So display error message
            self.__overlay.show_error_message(full_msg)
            
        self.__app.log_warning(full_msg)


    def __on_worker_signal(self, uid, data):
        """
        Signaled whenever the worker completes something.
        This method will dispatch the work to different methods
        depending on what async task has completed.
        """
        if self.__current_work_id == uid:
            # our publish data has arrived from sg!
            sg_data = data["sg"]
            self.__on_sg_data_arrived(sg_data)
        
        elif uid in self.__thumb_map:
            # a thumbnail is now present on disk!
            thumbnail_path = data["thumb_path"]
            
            item = self.__thumb_map[uid]["item"]
            sg_field = self.__thumb_map[uid]["field"]
            
            # call our deriving class implementation
            self._populate_thumbnail(item, sg_field, thumbnail_path)
            
    def __on_sg_data_arrived(self, sg_data):
        """
        Signaled whenever the worker completes something
        """
        
        # QT is struggling to handle the special timezone class that the shotgun API returns.
        # in fact, on linux it is struggling to serialize any complex object via QDataStream.
        #
        # Convert time stamps to unix time. Note that we lose any time zone qualifications by
        # by doing this - it is the the receiver's resposibility to handle this data correctly.
        # generally speaking, local timezone objects are returned by shotgun by default and 
        # this is how toolkit also initializes any shotgun connection it is making.
                
        for idx in range(len(sg_data)):
            for k in sg_data[idx]:
                if isinstance(sg_data[idx][k], datetime.datetime):
                    # convert to unix timestamp, local time zone
                    sg_data[idx][k] = time.mktime(sg_data[idx][k].timetuple())
        
        modifications_made = False
        
        # make sure no messages are displayed
        self.__overlay.hide()
    
        # pre-process
        sg_data = self._before_data_processing(sg_data)
    
        if len(self.__entity_tree_data) == 0:
            # we have an empty tree. Run recursive tree generation for performance.
            if len(sg_data) != 0:
                self.__app.log_debug("No cached items in tree! Creating full tree from Shotgun data...")
                self.__rebuild_whole_tree_from_sg_data(sg_data)
                self.__app.log_debug("...done!")
                modifications_made = True
        
        else:
            # go through and see if there are any changes we should apply to the tree.
            # note that there may be items 
            
            # check if anything has been deleted or added
            ids_from_shotgun = set([ d["id"] for d in sg_data ])
            ids_in_tree = set(self.__entity_tree_data.keys())
            removed_ids = ids_in_tree.difference(ids_from_shotgun)
            added_ids = ids_from_shotgun.difference(ids_in_tree)

            if len(removed_ids) > 0:
                self.__app.log_debug("Detected deleted items %s. Rebuilding tree..." % removed_ids)
                self.__rebuild_whole_tree_from_sg_data(sg_data)
                self.__app.log_debug("...done!")
                modifications_made = True
                
            elif len(added_ids) > 0:
                # wedge in the new items
                self.__app.log_debug("Detected added items. Adding them in-situ to tree...")
                for d in sg_data:
                    if d["id"] in added_ids:
                        self.__app.log_debug("Adding %s to tree" % d )
                        self.__add_sg_item_to_tree(d)
                self.__app.log_debug("...done!")
                modifications_made = True

            # check for modifications. At this point, the number of items in the tree and 
            # the sg data should match, except for any duplicate items in the tree which would 
            # effectively shadow each other. These can be safely ignored.
            #
            # Also note that we need to exclude any S3 urls from the comparison as these change
            # all the time
            #
            self.__app.log_debug("Checking for modifications...")
            detected_changes = False
            for d in sg_data:
                # if there are modifications of any kind, we just rebuild the tree at the moment
                try:
                    existing_sg_data = self.__entity_tree_data[ d["id"] ].data(ShotgunModel.SG_DATA_ROLE)
                    if not self.__sg_compare_data(d, existing_sg_data):                    
                        # shotgun data has changed for this item! Rebuild the tree
                        self.__app.log_debug("SG data change: %s --> %s" % (existing_sg_data, d))
                        detected_changes = True
                except KeyError, e:
                    self.__app.log_warning("Shotgun item %s not appearing in tree - most likely because "
                                          "there is another object in Shotgun with the same name." % d)
                      
            if detected_changes:
                self.__app.log_debug("Detected modifications. Rebuilding tree...")
                self.__rebuild_whole_tree_from_sg_data(sg_data)
                self.__app.log_debug("...done!")
                modifications_made = True
            else:
                self.__app.log_debug("...no modifications found.")
        
        # now go through the tree and download all thumbs
        
        # last step - save our tree to disk for fast caching next time!
        if modifications_made:
            self.__app.log_debug("Saving tree to disk %s..." % self.__full_cache_path)
            try:
                self.__save_to_disk(self.__full_cache_path)
                self.__app.log_debug("...saving complete!")            
            except Exception, e:
                self.__app.log_warning("Couldn't save cache data to disk: %s" % e)
        
        
    ########################################################################################
    # shotgun data processing and tree building
    
    def __utf8_to_unicode(self, sg_data):
        """
        Converts all strings values in this sg dictionary to unicode:
        
        in:  {"a":"aaa", "b": 123, "c": {"x":"y", "z":"aa"}, "d": [ {"x":"y", "z":"aa"} ] }
        out: {'a': u'aaa', 'c': {'x': u'y', 'z': u'aa'}, 'b': 123, 'd': [{'x': u'y', 'z': u'aa'}]}
        """
        new_sg_data = {}
        for x in sg_data:
            val = sg_data[x]
            if isinstance(val, list):
                new_sg_data[x] = [ self.__utf8_to_unicode(d) for d in val ]
            elif isinstance(val, dict):
                new_sg_data[x] = self.__utf8_to_unicode(val)
            elif isinstance(val, str):
                new_sg_data[x] = val.decode("UTF-8")
            else:
                new_sg_data[x] = val
        return new_sg_data
                
    
    
    def __sg_compare_data(self, a, b):
        """
        Compare two sg dicts:
        - unicode is turned into utf-8
        - assumes same set of keys in a and b
        - omits thumbnail fields because these change all the time (S3)
        """
        
        def _to_utf8(val):
            """
            Convert sg val to string.
            """
            if isinstance(val, unicode):
                # u"foo" --> "foo"
                str_val = val.encode('UTF-8')
            elif isinstance(val, dict):
                # assume sg link dict - convert name to str
                # {"id": 123, "name": u"foo"} ==> "foo"
                str_val = _to_utf8(val["name"])
            else:
                # 1 ==> "1"
                # "foo" ==> "foo"
                str_val = str(val)
            
            return str_val
            
        for k in a:
            
            # seem to have multiple url formats coming back from sg api so need to try to 
            # catchall time stamps and crypt keys because they keep changing all the time
            if "image" in k or "amazonaws" in _to_utf8(a[k]) or "AccessKeyId" in _to_utf8(a[k]):
                # skip thumbnail fields in the comparison - these 
                # change all the time!
                continue
            
            # now convert field values to strings and then comapre
            a_val = _to_utf8(a[k])
            b_val = _to_utf8(b[k])
            
            if a_val != b_val:
                return False

        return True

    def __add_sg_item_to_tree(self, sg_item):
        """
        Add a single item to the tree.
        This is a slow method.
        """
        root = self.invisibleRootItem()
        # now drill down recursively, create any missing nodes on the way
        # and eventually add this as a leaf item
        self.__add_sg_item_to_tree_r(sg_item, root, self.__hierarchy)
    
    
    def __add_sg_item_to_tree_r(self, sg_item, root, hierarchy):
        """
        Add a shotgun item to the tree. Create intermediate nodes if neccessary. 
        """
        # get the next field to display in tree view
        field = hierarchy[0]
        
        # get lower levels of values
        remaining_fields = hierarchy[1:]
        
        # are we at leaf level or not?
        on_leaf_level = len(remaining_fields) == 0

        # get the item we need at this level. Create it if not found.
        field_display_name = self.__sg_field_value_to_str(sg_item[field])
        found_item = None
        for row_index in range(root.rowCount()):
            child = root.child(row_index)

            if on_leaf_level:
                # compare shotgun ids
                sg_data = child.data(ShotgunModel.SG_DATA_ROLE)
                if sg_data.get("id") == sg_item.get("id"):
                    found_item = child
                    break
            else:
                # not on leaf level. Just compare names            
                if str(child.text()) == field_display_name:
                    found_item = child
                    break
        
        if found_item is None:
            # didn't find item! Create it!
            found_item = QtGui.QStandardItem(field_display_name)
            # keep tabs of which items we are creating
            found_item.setData(True, ShotgunModel.IS_SG_MODEL_ROLE)
            # keep a reference to this object to make GC happy
            # (pyside may crash otherwise)
            self.__all_tree_items.append(found_item)
            # and add to tree
            root.appendRow(found_item)

            # store the actual value we have
            found_item.setData({"name": field, "value": sg_item[field] }, 
                               ShotgunModel.SG_ASSOCIATED_FIELD_ROLE)
        
            if on_leaf_level:                
                # this is the leaf level!
                # attach the shotgun data so that we can access it later
                # note: QT automatically changes everything to be unicode
                # according to strange rules of its own, so force convert
                # all shotgun values to be proper unicode prior to setData
                found_item.setData(self.__utf8_to_unicode(sg_item), ShotgunModel.SG_DATA_ROLE)
                
                
                # set the default thumbnail
                self._populate_default_thumbnail(found_item)
                
                # call out to class implementation to do its thing
                self._populate_item(found_item, sg_item)
                
                # request thumb
                if self.__download_thumbs:
                    self.__process_thumbnail_for_item(found_item)
                                
                # and also populate the id association in our lookup dict
                self.__entity_tree_data[ sg_item["id"] ] = found_item
                
            else:
                # set the default thumbnail
                self._populate_default_thumbnail(found_item)
                
                # call out to class implementation to do its thing
                self._populate_item(found_item, None)


        if not on_leaf_level:
            # there are more levels that we should recurse down into
            self.__add_sg_item_to_tree_r(sg_item, found_item, remaining_fields)
        
    
    def __process_thumbnail_for_item(self, item):
        """
        Schedule a thumb download for an item
        """
        sg_data = item.data(ShotgunModel.SG_DATA_ROLE)
        
        for field in sg_data.keys():
        
            if "image" in field and sg_data[field] is not None:
                # we have a thumb we are supposed to download!
                # get the thumbnail - store the unique id we get back from
                # the data retrieve in a dict for fast lookup later
                uid = self.__sg_data_retriever.request_thumbnail(sg_data[field], 
                                                                 sg_data["type"], 
                                                                 sg_data["id"],
                                                                 field)
                
                self.__thumb_map[uid] = {"item": item, "field": field }
            
    
    def __rebuild_whole_tree_from_sg_data(self, data):
        """
        Clears the tree and rebuilds it from the given shotgun data.
        Note that any selection and expansion states in the view will be lost.
        """
        self._reset_all_data()
        
        # get any external payload from deriving classes
        self._load_external_data()
        
        # and add the shotgun data
        root = self.invisibleRootItem()
        self.__populate_complete_tree_r(data, root, self.__hierarchy, {})
        
    def __populate_complete_tree_r(self, sg_data, root, hierarchy, constraints):
        """
        Generate tree model data structure based on Shotgun data 
        """
        # get the next field to display in tree view
        field = hierarchy[0]
        # get lower levels of values
        remaining_fields = hierarchy[1:] 
        # are we at leaf level or not?
        on_leaf_level = len(remaining_fields) == 0
        
        # first pass, go through all our data, eliminate by 
        # constraints and get a result set.
        
        # the filtered_results list will contain a subset of the total data 
        # that is all matching the current constraints
        filtered_results = list()
        # maintain a list of unique matches for our current hierarchy field
        # for example, if the current level of the hierarchy is "asset type",
        # there will be more than one sg record having asset type = vehicle.
        discrete_values = {}
        
        for d in sg_data:
            
            # is this item matching the given constraints?
            if self.__check_constraints(d, constraints):
                # add this sg data dictionary to our list of matching results
                filtered_results.append(d)
                
                # and store it in our unique dictionary
                field_display_name = self.__sg_field_value_to_str(d[field])
                # and associate the shotgun data so that we can find it later
                
                if on_leaf_level and field_display_name in discrete_values:
                    # if we are on the leaf level, we want to make sure all objects
                    # are displayed! handle duplicates by appending the sg id to the name.
                    field_display_name = "%s (id %s)" % (field_display_name, d["id"])

                discrete_values[ field_display_name ] = d
                
            
        for dv in sorted(discrete_values.keys()):
            
            # construct tree view node object
            item = QtGui.QStandardItem(dv)
            # keep tabs of which items we are creating
            item.setData(True, ShotgunModel.IS_SG_MODEL_ROLE)
            # keep a reference to this object to make GC happy
            # (pyside may crash otherwise)
            self.__all_tree_items.append(item)            
            root.appendRow(item)
            
            # get the full sg data dict that corresponds to this folder item
            # note that this item may only partially match the sg data
            # for leaf item, the sg_item completely matches the item
            # but higher up it will be a subset of the fields only.
            sg_item = discrete_values[dv]
            
            # store the actual field value we have for this item
            item.setData({"name": field, "value": sg_item[field] }, 
                         ShotgunModel.SG_ASSOCIATED_FIELD_ROLE)
            
                        
            if on_leaf_level:
                
                # this is the leaf level
                # attach the shotgun data so that we can access it later
                # note - pyqt converts everything automatically to unicode,
                # but using somewhat strange rules, so properly convert
                # values to unicode prior to insertion
                item.setData(self.__utf8_to_unicode(sg_item), ShotgunModel.SG_DATA_ROLE)

                # set the default thumbnail
                self._populate_default_thumbnail(item)
                
                # call out to class implementation to do its thing
                self._populate_item(item, sg_item)                
                
                # request thumb
                if self.__download_thumbs:
                    self.__process_thumbnail_for_item(item)                
                
                # and also populate the id association in our lookup dict
                self.__entity_tree_data[ sg_item["id"] ] = item      
            else:
                
                # not on leaf level yet
                # set the default thumbnail
                self._populate_default_thumbnail(item)
                
                # call out to class implementation to do its thing
                self._populate_item(item, None)
                
                # now when we recurse down, we need to add our current constrain
                # to the list of constraints. For this we need the raw sg value
                # and now the display name that we used when we constructed the
                # tree node. 
                new_constraints = {}
                new_constraints.update(constraints)
                new_constraints[field] = discrete_values[dv][field]
                
                # and process subtree
                self.__populate_complete_tree_r(filtered_results, 
                                               item, 
                                               remaining_fields, 
                                               new_constraints)
                
            
    def __check_constraints(self, record, constraints):
        """
        checks if a particular shotgun record is matching the given 
        constraints dictionary. Returns if the constraints dictionary 
        is not a subset of the record dictionary. 
        """
        for constraint_field in constraints:
            if constraints[constraint_field] != record[constraint_field]:
                return False
        return True
            
    def __sg_field_value_to_str(self, value):
        """
        Turns a shotgun value to a string. This method receives values
        that are coming straight from the Shotgun API so they are 
        string based and not unicode based.
        """
        if isinstance(value, dict) and "name" in value:
            # linked fields
            return str(value["name"])
        else:
            # everything else
            return str(value)
            
    ########################################################################################
    # de/serialization of model contents 
            
    def __save_to_disk(self, filename):
        """
        Save the model to disk
        """
        fh = QtCore.QFile(filename)
        fh.open(QtCore.QIODevice.WriteOnly);
        out = QtCore.QDataStream(fh)
        
        # write a header
        out.writeInt64(FILE_MAGIC_NUMBER)
        out.writeInt32(FILE_VERSION)

        # tell which serialization dialect to use
        out.setVersion(QtCore.QDataStream.Qt_4_0)

        root = self.invisibleRootItem()
        
        self.__save_to_disk_r(out, root, 0)
        
    def __save_to_disk_r(self, stream, item, depth):
        """
        Recursive tree writer
        """
        num_rows = item.rowCount()
        for row in range(num_rows):
            # write this
            child = item.child(row)
            # only write shotgun data!
            # data from external sources is never serialized
            if child.data(ShotgunModel.IS_SG_MODEL_ROLE):
                child.write(stream)
                stream.writeInt32(depth)
            
            if child.hasChildren():
                # write children
                self.__save_to_disk_r(stream, child, depth+1)                
            

    def __load_from_disk(self, filename):
        """
        Load a serialized model from disk
        """
        fh = QtCore.QFile(filename)
        fh.open(QtCore.QIODevice.ReadOnly);
        file_in = QtCore.QDataStream(fh)
        
        magic = file_in.readInt64()
        if magic != FILE_MAGIC_NUMBER:
            raise Exception("Invalid file magic number!")
        
        version = file_in.readInt32()
        if version != FILE_VERSION:
            raise Exception("Invalid file version!")
        
        # tell which deserialization dialect to use
        file_in.setVersion(QtCore.QDataStream.Qt_4_0)
        
        curr_parent = self.invisibleRootItem()
        prev_node = None
        curr_depth = 0
        
        while not file_in.atEnd():
        
            # read data
            item = QtGui.QStandardItem()
            # keep a reference to this object to make GC happy
            # (pyside may crash otherwise)
            self.__all_tree_items.append(item)
            item.read(file_in)
            node_depth = file_in.readInt32()
            
            # all leaf nodes have an sg id stored in their metadata
            # the role data accessible via item.data() contains the sg id for this item
            # if there is a sg id associated with this node
            if item.data(ShotgunModel.SG_DATA_ROLE):
                sg_data = item.data(ShotgunModel.SG_DATA_ROLE) 
                # add the model item to our tree data dict keyed by id
                self.__entity_tree_data[ sg_data["id"] ] = item            

            # serialized items do not contain a full high rez thumb, so 
            # re-create that. First, set the default thumbnail
            self._populate_default_thumbnail(item)

            # request thumb
            if self.__download_thumbs:
                self.__process_thumbnail_for_item(item)
                    
            if node_depth == curr_depth + 1:
                # this new node is a child of the previous node
                curr_parent = prev_node
                if prev_node is None:
                    raise Exception("File integrity issues!")
                curr_depth = node_depth
            
            elif node_depth > curr_depth + 1:
                # something's wrong!
                raise Exception("File integrity issues!") 
            
            elif node_depth < curr_depth:
                # we are going back up to parent level
                while curr_depth > node_depth:
                    curr_depth = curr_depth -1
                    curr_parent = curr_parent.parent()
                    if curr_parent is None:
                        # we reached the root. special case
                        curr_parent = self.invisibleRootItem()
        
            # and attach the node
            curr_parent.appendRow(item)
            prev_node = item
            
