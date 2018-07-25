import argparse
import codecs
import functools
import os.path
import re
import sys
import warnings
import webbrowser

from qtpy import QtCore
from qtpy.QtCore import Qt
from qtpy import QtGui
from qtpy import QtWidgets

from labelme import __appname__
from labelme import __version__
from labelme.canvas import Canvas
from labelme.colorDialog import ColorDialog
from labelme.config import get_config
from labelme.labelDialog import LabelDialog
from labelme.labelFile import LabelFile
from labelme.labelFile import LabelFileError
from labelme.lib import addActions
from labelme.lib import fmtShortcut
from labelme.lib import newAction
from labelme.lib import newIcon
from labelme.lib import struct
from labelme import logger
from labelme import QT5
from labelme.shape import DEFAULT_FILL_COLOR
from labelme.shape import DEFAULT_LINE_COLOR
from labelme.shape import Shape
from labelme.toolBar import ToolBar
from labelme.zoomWidget import ZoomWidget

QtCore.QPoint = QtCore.QPointF # we will use float points

# FIXME
# - [medium] Set max zoom value to something big enough for FitWidth/Window

# TODO(unknown):
# - [high] Add polygon movement with arrow keys
# - [high] Deselect shape when clicking and already selected(?)
# - [low,maybe] Open images with drag & drop.
# - [low,maybe] Preview images on file dialogs.
# - Zoom is too "steppy".


# Utility functions and classes.
from urllib.request import urlopen

class WindowMixin(object):
    def menu(self, title, actions=None):
        menu = self.menuBar().addMenu(title)
        if actions:
            addActions(menu, actions)
        return menu

    def toolbar(self, title, actions=None):
        toolbar = ToolBar(title)
        toolbar.setObjectName('%sToolBar' % title)
        # toolbar.setOrientation(Qt.Vertical)
        toolbar.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        if actions:
            addActions(toolbar, actions)
        self.addToolBar(Qt.LeftToolBarArea, toolbar)
        return toolbar


class EscapableQListWidget(QtWidgets.QListWidget):

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.clearSelection()


class LabelQListWidget(QtWidgets.QListWidget):

    def __init__(self, *args, **kwargs):
        super(LabelQListWidget, self).__init__(*args, **kwargs)
        self.canvas = None
        self.itemsToShapes = []

    def get_shape_from_item(self, item):
        for index, (item_, shape) in enumerate(self.itemsToShapes):
            if item_ is item:
                return shape

    def get_item_from_shape(self, shape):
        for index, (item, shape_) in enumerate(self.itemsToShapes):
            if shape_ is shape:
                return item

    def clear(self):
        super(LabelQListWidget, self).clear()
        self.itemsToShapes = []

    def setParent(self, parent):
        self.parent = parent

    def dropEvent(self, event):
        shapes = self.shapes
        super(LabelQListWidget, self).dropEvent(event)
        if self.shapes == shapes:
            return
        if self.canvas is None:
            raise RuntimeError('self.canvas must be set beforehand.')
        self.parent.setDirty()
        self.canvas.loadShapes(shapes)

    @property
    def shapes(self):
        shapes = []
        for i in range(self.count()):
            item = self.item(i)
            shape = self.get_shape_from_item(item)
            shapes.append(shape)
        return shapes


class MainWindow(QtWidgets.QMainWindow, WindowMixin):
    FIT_WINDOW, FIT_WIDTH, MANUAL_ZOOM = 0, 1, 2

    def __init__(self, config=None, filename=None, output=None):
        # see labelme/config/default_config.yaml for valid configuration
        if config is None:
            config = get_config()
        self._config = config

        super(MainWindow, self).__init__()
        self.setWindowTitle(__appname__)

        # Whether we need to save or not.
        self.dirty = False

        self._noSelectionSlot = False

        # Main widgets and related state.
        self.labelDialog = LabelDialog(
            parent=self,
            labels=self._config['labels'],
            sort_labels=self._config['sort_labels'],
            show_text_field=self._config['show_label_text_field'],
        )

        self.labelList = LabelQListWidget()

        self.targetDirPath = '' # the target path of images
        self.defaultSaveDir = None
        self.save_labelme_dir = save_labelme_dir = self._config['save_labelme_dir']
        self.lastOpenDir = None
        self.sort_type_and_flag_backup = None
        self.image_path_stats_dict_list_backup = dict()
        self.image_path_list_last = list()
        self.image_path_labelme_file_dict = dict()

        if save_labelme_dir is not None and os.path.exists(save_labelme_dir):
            self.statusBar().showMessage('%s started. Annotation will be saved to %s' %
                                         (__appname__, save_labelme_dir))
            self.statusBar().show()

        self.labelList.itemActivated.connect(self.labelSelectionChanged)
        self.labelList.itemSelectionChanged.connect(self.labelSelectionChanged)
        self.labelList.itemDoubleClicked.connect(self.editLabel)
        # Connect to itemChanged to detect checkbox changes.
        self.labelList.itemChanged.connect(self.labelItemChanged)
        self.labelList.setDragDropMode(
            QtWidgets.QAbstractItemView.InternalMove)
        self.labelList.setParent(self)

        listLayout = QtWidgets.QVBoxLayout()
        listLayout.setContentsMargins(0, 0, 0, 0)
        self.editButton = QtWidgets.QToolButton()
        self.editButton.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        listLayout.addWidget(self.editButton)  # 0, Qt.AlignCenter)
        listLayout.addWidget(self.labelList)
        self.labelListContainer = QtWidgets.QWidget()
        self.labelListContainer.setLayout(listLayout)

        self.flag_dock = self.flag_widget = None
        self.flag_dock = QtWidgets.QDockWidget('Flags', self)
        self.flag_dock.setObjectName('Flags')
        self.flag_widget = QtWidgets.QListWidget()
        if config['flags']:
            self.loadFlags({k: False for k in config['flags']})
        self.flag_dock.setWidget(self.flag_widget)
        # self.flag_widget.itemChanged.connect(self.setDirty)
        self.flag_widget.itemChanged.connect(self.flag_item_changed)


        self.sort_file_dock = self.sort_file_widget = None
        self.sort_file_dock = QtWidgets.QDockWidget('Sort Flags', self)
        self.sort_file_dock.setObjectName('Sort Flags')
        self.sort_file_widget = QtWidgets.QListWidget()
        if config['sort_files_choices']:
            self.loadSortFlags({k: False for k in config['sort_files_choices']})
        self.sort_file_dock.setWidget(self.sort_file_widget)
        # self.flag_widget.itemChanged.connect(self.setDirty)
        self.sort_file_widget.itemChanged.connect(self.sort_type_flag_item_changed)

        self.uniqLabelList = EscapableQListWidget()
        self.uniqLabelList.setToolTip(
            "Select label to start annotating for it. "
            "Press 'Esc' to deselect.")
        if self._config['labels']:
            self.uniqLabelList.addItems(self._config['labels'])
            self.uniqLabelList.sortItems()
        self.labelsdock = QtWidgets.QDockWidget(u'Label List', self)
        self.labelsdock.setObjectName(u'Label List')
        self.labelsdock.setWidget(self.uniqLabelList)

        self.dock = QtWidgets.QDockWidget('Polygon Labels', self)
        self.dock.setObjectName('Labels')
        self.dock.setWidget(self.labelListContainer)

        self.fileListWidget = QtWidgets.QListWidget()
        self.fileListWidget.itemSelectionChanged.connect(
            self.fileSelectionChanged)
        filelistLayout = QtWidgets.QVBoxLayout()
        filelistLayout.setContentsMargins(0, 0, 0, 0)
        filelistLayout.addWidget(self.fileListWidget)
        fileListContainer = QtWidgets.QWidget()
        fileListContainer.setLayout(filelistLayout)
        self.filedock = QtWidgets.QDockWidget(u'File List', self)
        self.filedock.setObjectName(u'Files')
        self.filedock.setWidget(fileListContainer)

        self.zoomWidget = ZoomWidget()
        self.colorDialog = ColorDialog(parent=self)

        self.canvas = self.labelList.canvas = Canvas()
        self.canvas.zoomRequest.connect(self.zoomRequest)

        scrollArea = QtWidgets.QScrollArea()
        scrollArea.setWidget(self.canvas)
        scrollArea.setWidgetResizable(True)
        self.scrollBars = {
            Qt.Vertical: scrollArea.verticalScrollBar(),
            Qt.Horizontal: scrollArea.horizontalScrollBar(),
        }
        self.canvas.scrollRequest.connect(self.scrollRequest)

        self.canvas.newShape.connect(self.newShape)
        self.canvas.shapeMoved.connect(self.setDirty)
        self.canvas.selectionChanged.connect(self.shapeSelectionChanged)
        self.canvas.drawingPolygon.connect(self.toggleDrawingSensitive)

        self.setCentralWidget(scrollArea)

        self.addDockWidget(Qt.RightDockWidgetArea, self.sort_file_dock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.flag_dock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.labelsdock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.dock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.filedock)
        self.filedock.setFeatures(QtWidgets.QDockWidget.DockWidgetFloatable)

        self.dockFeatures = (QtWidgets.QDockWidget.DockWidgetClosable |
                             QtWidgets.QDockWidget.DockWidgetFloatable)
        self.dock.setFeatures(self.dock.features() ^ self.dockFeatures)

        # Actions
        action = functools.partial(newAction, self)
        shortcuts = self._config['shortcuts']
        quit = action('&Quit', self.close, shortcuts['quit'], 'quit',
                      'Quit application')
        open_ = action('&Open', self.openFile, shortcuts['open'], 'open',
                       'Open image or label file')
        opendir = action('&Open Dir', self.openDirDialog,
                         shortcuts['open_dir'], 'open', u'Open Dir')

        changeSavedir = action('&Change Save Dir', self.changeSavedirDialog,
                               shortcuts['change_save_dir'], 'open', u'Change default saved Annotation dir')

        openNextImg = action('&Next Image', self.openNextImg,
                             shortcuts['open_next'], 'next', u'Open Next')

        openPrevImg = action('&Prev Image', self.openPrevImg,
                             shortcuts['open_prev'], 'prev', u'Open Prev')

        verify = action('&Verify Image', self.verifyImg,
                        'space', 'verify', u'Verify Image')

        save = action('&Save', self.saveFile, shortcuts['save'], 'save',
                      'Save labels to file', enabled=False)
        saveAs = action('&Save As', self.saveFileAs, shortcuts['save_as'],
                        'save-as', 'Save labels to a different file',
                        enabled=False)
        close = action('&Close', self.closeFile, shortcuts['close'], 'close',
                       'Close current file')
        color1 = action('Polygon &Line Color', self.chooseColor1,
                        shortcuts['edit_line_color'], 'color_line',
                        'Choose polygon line color')
        color2 = action('Polygon &Fill Color', self.chooseColor2,
                        shortcuts['edit_fill_color'], 'color',
                        'Choose polygon fill color')

        createMode = action('Create\nPolygo&ns', self.setCreateMode,
                            shortcuts['create_polygon'], 'objects',
                            'Start drawing polygons', enabled=True)
        createRectangleMode = action(
            'Create\nRectangle', self.setCreateRectangleMode,
            shortcuts['create_rectangle'], 'objects',
            'Start drawing rectangles', enabled=True)
        editMode = action('&Edit\nPolygons', self.setEditMode,
                          shortcuts['edit_polygon'], 'edit',
                          'Move and edit polygons', enabled=True)
        editRectangleMode = action('&Edit\nRectangle', self.setEditRectangleMode,
                          shortcuts['edit_rectangle'], 'edit',
                          'Move and edit rectangle', enabled=True)

        delete = action('Delete\nPolygon', self.deleteSelectedShape,
                        shortcuts['delete_polygon'], 'cancel',
                        'Delete', enabled=False)
        copy = action('&Duplicate\nPolygon', self.copySelectedShape,
                      shortcuts['duplicate_polygon'], 'copy',
                      'Create a duplicate of the selected polygon',
                      enabled=False)
        undoLastPoint = action('Undo last point', self.canvas.undoLastPoint,
                               shortcuts['undo_last_point'], 'undo',
                               'Undo last drawn point', enabled=False)
        addPoint = action('Add Point to Edge', self.canvas.addPointToEdge,
                          None, 'edit', 'Add point to the nearest edge',
                          enabled=False)

        undo = action('Undo', self.undoShapeEdit, shortcuts['undo'], 'undo',
                      'Undo last add and edit of shape', enabled=False)

        hideAll = action('&Hide\nPolygons',
                         functools.partial(self.togglePolygons, False),
                         icon='eye', tip='Hide all polygons', enabled=False)
        showAll = action('&Show\nPolygons',
                         functools.partial(self.togglePolygons, True),
                         icon='eye', tip='Show all polygons', enabled=False)

        help = action('&Tutorial', self.tutorial, icon='help',
                      tip='Show tutorial page')

        zoom = QtWidgets.QWidgetAction(self)
        zoom.setDefaultWidget(self.zoomWidget)
        self.zoomWidget.setWhatsThis(
            "Zoom in or out of the image. Also accessible with"
            " %s and %s from the canvas." %
            (fmtShortcut('%s,%s' % (shortcuts['zoom_in'],
                                    shortcuts['zoom_out'])),
             fmtShortcut("Ctrl+Wheel")))
        self.zoomWidget.setEnabled(False)

        zoomIn = action('Zoom &In', functools.partial(self.addZoom, 10),
                        shortcuts['zoom_in'], 'zoom-in',
                        'Increase zoom level', enabled=False)
        zoomOut = action('&Zoom Out', functools.partial(self.addZoom, -10),
                         shortcuts['zoom_out'], 'zoom-out',
                         'Decrease zoom level', enabled=False)
        zoomOrg = action('&Original size',
                         functools.partial(self.setZoom, 100),
                         shortcuts['zoom_to_original'], 'zoom',
                         'Zoom to original size', enabled=False)
        fitWindow = action('&Fit Window', self.setFitWindow,
                           shortcuts['fit_window'], 'fit-window',
                           'Zoom follows window size', checkable=True,
                           enabled=False)
        fitWidth = action('Fit &Width', self.setFitWidth,
                          shortcuts['fit_width'], 'fit-width',
                          'Zoom follows window width',
                          checkable=True, enabled=False)
        # Group zoom controls into a list for easier toggling.
        zoomActions = (self.zoomWidget, zoomIn, zoomOut, zoomOrg,
                       fitWindow, fitWidth)
        self.zoomMode = self.MANUAL_ZOOM
        self.scalers = {
            self.FIT_WINDOW: self.scaleFitWindow,
            self.FIT_WIDTH: self.scaleFitWidth,
            # Set to one to scale to 100% when loading files.
            self.MANUAL_ZOOM: lambda: 1,
        }

        edit = action('&Edit Label', self.editLabel, shortcuts['edit_label'],
                      'edit', 'Modify the label of the selected polygon',
                      enabled=False)
        self.editButton.setDefaultAction(edit)

        shapeLineColor = action(
            'Shape &Line Color', self.chshapeLineColor, icon='color-line',
            tip='Change the line color for this specific shape', enabled=False)
        shapeFillColor = action(
            'Shape &Fill Color', self.chshapeFillColor, icon='color',
            tip='Change the fill color for this specific shape', enabled=False)

        labels = self.dock.toggleViewAction()
        labels.setText('Show/Hide Label Panel')

        # Lavel list context menu.
        labelMenu = QtWidgets.QMenu()
        addActions(labelMenu, (edit, delete))
        self.labelList.setContextMenuPolicy(Qt.CustomContextMenu)
        self.labelList.customContextMenuRequested.connect(
            self.popLabelListMenu)

        # Store actions for further handling.
        self.actions = struct(
            save=save, saveAs=saveAs, open=open_, close=close,
            lineColor=color1, fillColor=color2,
            delete=delete, edit=edit, copy=copy,
            undoLastPoint=undoLastPoint, undo=undo,
            addPoint=addPoint,
            createMode=createMode, editMode=editMode,
            createRectangleMode=createRectangleMode, editRectangleMode=editRectangleMode,
            shapeLineColor=shapeLineColor, shapeFillColor=shapeFillColor,
            zoom=zoom, zoomIn=zoomIn, zoomOut=zoomOut, zoomOrg=zoomOrg,
            fitWindow=fitWindow, fitWidth=fitWidth,
            zoomActions=zoomActions,
            fileMenuActions=(open_, opendir, save, saveAs, close, quit),
            tool=(),
            editMenu=(edit, copy, delete, None, undo, undoLastPoint,
                      None, color1, color2),
            menu=(
                createMode, createRectangleMode,
                editMode, editRectangleMode, edit, copy,
                delete, shapeLineColor, shapeFillColor,
                undo, undoLastPoint, addPoint,
            ),
            onLoadActive=(close, createMode, createRectangleMode, editMode, editRectangleMode),
            onShapesPresent=(saveAs, hideAll, showAll),
        )

        self.canvas.edgeSelected.connect(self.actions.addPoint.setEnabled)

        self.menus = struct(
            file=self.menu('&File'),
            edit=self.menu('&Edit'),
            view=self.menu('&View'),
            help=self.menu('&Help'),
            recentFiles=QtWidgets.QMenu('Open &Recent'),
            labelList=labelMenu,
        )

        addActions(self.menus.file, (open_, opendir, changeSavedir, self.menus.recentFiles,
                                     save, saveAs, close, None, quit))
        addActions(self.menus.help, (help,))
        addActions(self.menus.view, (
            labels, None,
            hideAll, showAll, None,
            zoomIn, zoomOut, zoomOrg, None,
            fitWindow, fitWidth))

        self.menus.file.aboutToShow.connect(self.updateFileMenu)

        # Custom context menu for the canvas widget:
        addActions(self.canvas.menus[0], self.actions.menu)
        addActions(self.canvas.menus[1], (
            action('&Copy here', self.copyShape),
            action('&Move here', self.moveShape)))

        self.tools = self.toolbar('Tools')
        # Menu buttons on Left
        self.actions.tool = (
            open_,
            opendir,
            changeSavedir,
            openNextImg,
            openPrevImg,
            verify,
            save,
            None,
            createMode,
            createRectangleMode,
            editMode,
            editRectangleMode,
            copy,
            delete,
            undo,
            None,
            zoomIn,
            zoom,
            zoomOut,
            fitWindow,
            fitWidth,
        )

        self.statusBar().showMessage('%s started.' % __appname__)
        self.statusBar().show()

        # Application state.
        self.image = QtGui.QImage()
        self.imagePath = None
        if self._config['auto_save'] and output is not None:
            warnings.warn('If `auto_save` argument is True, `output` argument '
                          'is ignored and output filename is automatically '
                          'set as IMAGE_BASENAME.json.')
        self.labeling_once = output is not None
        self.output = output
        self.recentFiles = []
        self.maxRecent = 7
        self.lineColor = None
        self.fillColor = None
        self.otherData = None
        self.zoom_level = 100
        self.fit_window = False

        if filename is not None and os.path.isdir(filename):
            self.importDirImages(filename, load=False)
        else:
            self.filename = filename

        # XXX: Could be completely declarative.
        # Restore application settings.
        self.settings = QtCore.QSettings('labelme', 'labelme')
        # FIXME: QSettings.value can return None on PyQt4
        self.recentFiles = self.settings.value('recentFiles', []) or []
        size = self.settings.value('window/size', QtCore.QSize(600, 500))
        position = self.settings.value('window/position', QtCore.QPoint(0, 0).toPoint())
        self.resize(size)
        self.move(position)
        # or simply:
        # self.restoreGeometry(settings['window/geometry']
        self.restoreState(
            self.settings.value('window/state', QtCore.QByteArray()))
        self.lineColor = QtGui.QColor(
            self.settings.value('line/color', Shape.line_color))
        self.fillColor = QtGui.QColor(
            self.settings.value('fill/color', Shape.fill_color))
        Shape.line_color = self.lineColor
        Shape.fill_color = self.fillColor

        # Populate the File menu dynamically.
        self.updateFileMenu()
        # Since loading the file may take some time,
        # make sure it runs in the background.
        if self.filename is not None:
            self.queueEvent(functools.partial(self.loadFile, self.filename))

        # Callbacks:
        self.zoomWidget.valueChanged.connect(self.paintCanvas)

        self.populateModeActions()


        image_dir = self._config['image_dir']
        if image_dir is not None:
            self.openDirDialog(dirpath=image_dir, _value=True)
        elif image_dir is None and save_labelme_dir is not None:
            self.openDirDialog(dirpath=save_labelme_dir, _value=True)

    # Support Functions

    def noShapes(self):
        return not self.labelList.itemsToShapes

    def populateModeActions(self):
        tool, menu = self.actions.tool, self.actions.menu
        self.tools.clear()
        addActions(self.tools, tool)
        self.canvas.menus[0].clear()
        addActions(self.canvas.menus[0], menu)
        self.menus.edit.clear()
        actions = (
            self.actions.createMode,
            self.actions.createRectangleMode,
            self.actions.editMode,
            self.actions.editRectangleMode
        )
        addActions(self.menus.edit, actions + self.actions.editMenu)

    def flag_item_changed(self):
        flags = self.load_current_flag(self.flag_widget)
        self.show_type_flag_item_changed(flags)

    def show_type_flag_item_changed(self, flags):
        not_allowed_types = []

        for flag, v in flags.items():
            if v == False:
                try:
                    flag = flag.split('|')[0]
                except:
                    pass
                not_allowed_types.append(flag)

        for shape in self.canvas.shapes:
            text = shape.label
            try:
                label_, label_id_ = text.split('~')
            except Exception as e:
                continue

            custom_data = self.labelFile.otherData.get('custom_data', dict())
            id_type_pair = custom_data.get('id_type_pair', dict())
            type_s = id_type_pair.get(str(label_id_), None)
            if type_s is None or type_s not in not_allowed_types:
                self.canvas.setShapeVisible(shape, True)
                self.togglePolygons_type(value=True, shape_chosen=shape)
            else:
                self.canvas.setShapeVisible(shape, False)
                self.togglePolygons_type(value=False, shape_chosen=shape)

    def sort_type_flag_item_changed(self):

        sort_type_and_flag = self.load_current_flag(self.sort_file_widget)

        if len(self.image_path_list_last) == 0 and len(self.image_path_stats_dict_list_backup) == 0: # there is no more than two images
            return

        image_path_list = list(self.image_path_stats_dict_list_backup)  # init

        if self.sort_type_and_flag_backup!=sort_type_and_flag:
            reversed_sort = sort_type_and_flag.pop('reversed_sort')
            for sort_type, sort_flag in sort_type_and_flag.items():
                if sort_flag:
                    image_path_list = self.change_image_path_list_order(order_by_type=sort_type)

            if reversed_sort:
                image_path_list = image_path_list[::-1]

        if image_path_list != self.image_path_list_last:
            self.import_changed_image_path_list(image_path_list)

        self.sort_type_and_flag_backup = sort_type_and_flag.copy()
        self.image_path_list_last = image_path_list.copy()

    def setDirty(self):
        if self._config['auto_save']:
            label_file = self.assign_labelfile_with_imagepath(self.imagePath)
            self.saveLabels(label_file)
            return
        self.dirty = True
        self.canvas.verified = False    # reset verified
        self.actions.save.setEnabled(True)
        self.actions.undo.setEnabled(self.canvas.isShapeRestorable)
        title = __appname__
        if self.filename is not None:
            title = '{} - {}*'.format(title, self.filename)
        self.setWindowTitle(title)

    def setClean(self):
        self.dirty = False
        self.actions.save.setEnabled(False)
        self.actions.createMode.setEnabled(True)
        self.actions.createRectangleMode.setEnabled(True)
        title = __appname__
        if self.filename is not None:
            title = '{} - {}'.format(title, self.filename)
        self.setWindowTitle(title)

    def toggleActions(self, value=True):
        """Enable/Disable widgets which depend on an opened image."""
        for z in self.actions.zoomActions:
            z.setEnabled(value)
        for action in self.actions.onLoadActive:
            action.setEnabled(value)

    def queueEvent(self, function):
        QtCore.QTimer.singleShot(0, function)

    def status(self, message, delay=5000):
        self.statusBar().showMessage(message, delay)

    def resetState(self):
        self.labelList.clear()
        self.filename = None
        self.imagePath = None
        self.imageData = None
        self.labelFile = None
        self.otherData = None
        self.canvas.resetState()

    def currentItem(self):
        items = self.labelList.selectedItems()
        if items:
            return items[0]
        return None

    def addRecentFile(self, filename):
        if filename in self.recentFiles:
            self.recentFiles.remove(filename)
        elif len(self.recentFiles) >= self.maxRecent:
            self.recentFiles.pop()
        self.recentFiles.insert(0, filename)

    # Callbacks

    def undoShapeEdit(self):
        self.canvas.restoreShape()
        self.labelList.clear()
        self.uniqLabelList.clear()
        self.loadShapes(self.canvas.shapes)
        self.actions.undo.setEnabled(self.canvas.isShapeRestorable)

    def tutorial(self):
        url = 'https://github.com/wkentaro/labelme/tree/master/examples/tutorial'  # NOQA
        webbrowser.open(url)

    def toggleAddPointEnabled(self, enabled):
        self.actions.addPoint.setEnabled(enabled)

    def toggleDrawingSensitive(self, drawing=True):
        """Toggle drawing sensitive.

        In the middle of drawing, toggling between modes should be disabled.
        """
        self.actions.editMode.setEnabled(not drawing)
        self.actions.editRectangleMode.setEnabled(not drawing)
        self.actions.undoLastPoint.setEnabled(drawing)
        self.actions.undo.setEnabled(not drawing)

    def toggleDrawMode(self, edit=True, createMode='polygon'):
        self.canvas.setEditing(edit)
        self.canvas.createMode = createMode

        if createMode == 'polygon':
            self.actions.createMode.setEnabled(edit)
            self.actions.createRectangleMode.setEnabled(not edit)
        elif createMode == 'rectangle':
            self.actions.createMode.setEnabled(not edit)
            self.actions.createRectangleMode.setEnabled(edit)
        else:
            raise ValueError
        self.actions.editMode.setEnabled(not edit)
        self.actions.editRectangleMode.setEnabled(not edit)

    def setCreateRectangleMode(self):
        self.toggleDrawMode(False, createMode='rectangle')

    def setCreateMode(self):
        self.toggleDrawMode(False, createMode='polygon')

    def setEditRectangleMode(self):
        self.toggleDrawMode(True, createMode='rectangle')

    def setEditMode(self):
        self.toggleDrawMode(True, createMode='polygon')

    def updateFileMenu(self):
        current = self.filename

        def exists(filename):
            return os.path.exists(str(filename))

        menu = self.menus.recentFiles
        menu.clear()
        files = [f for f in self.recentFiles if f != current and exists(f)]
        for i, f in enumerate(files):
            icon = newIcon('labels')
            action = QtWidgets.QAction(
                icon, '&%d %s' % (i + 1, QtCore.QFileInfo(f).fileName()), self)
            action.triggered.connect(functools.partial(self.loadRecent, f))
            menu.addAction(action)

    def popLabelListMenu(self, point):
        self.menus.labelList.exec_(self.labelList.mapToGlobal(point))

    def validateLabel(self, label):
        # no validation
        if self._config['validate_label'] is None:
            return True

        for i in range(self.uniqLabelList.count()):
            label_i = self.uniqLabelList.item(i).text()
            if self._config['validate_label'] in ['exact', 'instance']:
                if label_i == label:
                    return True
            if self._config['validate_label'] == 'instance':
                m = re.match(r'^{}-[0-9]*$'.format(label_i), label)
                if m:
                    return True
        return False

    def editLabel(self, item=None):
        if not self.canvas.editing():
            return

        if not self.confirmMessage("Editlabel", "Are you really want to edit the label?"):
            return

        item = item if item else self.currentItem()
        text = self.labelDialog.popUp(item.text())
        # text = item.text()

        try:
            from labelme.change_attrs_with_gui.change_labelme_attr_with_gui_interface import load_change_labelme_attr_with_gui
            change_labelme_attr_with_gui_class = load_change_labelme_attr_with_gui(self._config['change_labelme_attr_with_gui_file_path'])
            # WE WILL CHANGE THE CUSTOM DATA HERE
            change_labelme_attr_with_gui = change_labelme_attr_with_gui_class(self.labelFile, label_type=self._config['label_type'])
            label_, label_id_ = text.split('~')
            change_labelme_attr_with_gui.GUI(int(label_id_)) # note the int
        except Exception as e:
            msg = QtWidgets.QMessageBox()
            msg.setText("In load_change_labelme_attr_with_gui\n"+str(e))
            msg.exec_()
        if text is None:
            return
        if not self.validateLabel(text):
            self.errorMessage('Invalid label',
                              "Invalid label '{}' with validation type '{}'"
                              .format(text, self._config['validate_label']))
            return
        item.setText(text)
        self.setDirty()
        if not self.uniqLabelList.findItems(text, Qt.MatchExactly):
            self.uniqLabelList.addItem(text)
            self.uniqLabelList.sortItems()

    def fileSelectionChanged(self):
        items = self.fileListWidget.selectedItems()
        if not items:
            return
        item = items[0]

        if not self.mayContinue():
            return

        currIndex = self.imageList.index(str(item.text()))
        if currIndex < len(self.imageList):
            filename = self.imageList[currIndex]
            if filename:
                self.loadFile(filename)

    # React to canvas signals.
    def shapeSelectionChanged(self, selected=False):
        if self._noSelectionSlot:
            self._noSelectionSlot = False
        else:
            shape = self.canvas.selectedShape
            if shape:
                item = self.labelList.get_item_from_shape(shape)
                item.setSelected(True)
            else:
                self.labelList.clearSelection()
        self.actions.delete.setEnabled(selected)
        self.actions.copy.setEnabled(selected)
        self.actions.edit.setEnabled(selected)
        self.actions.shapeLineColor.setEnabled(selected)
        self.actions.shapeFillColor.setEnabled(selected)

    def addLabel(self, shape):
        item = QtWidgets.QListWidgetItem(shape.label)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked)
        self.labelList.itemsToShapes.append((item, shape))
        self.labelList.addItem(item)
        if not self.uniqLabelList.findItems(shape.label, Qt.MatchExactly):
            self.uniqLabelList.addItem(shape.label)
            self.uniqLabelList.sortItems()
        self.labelDialog.addLabelHistory(item.text())
        for action in self.actions.onShapesPresent:
            action.setEnabled(True)

    def remLabel(self, shape):
        item = self.labelList.get_item_from_shape(shape)
        self.labelList.takeItem(self.labelList.row(item))

    def loadShapes(self, shapes):
        for shape in shapes:
            self.addLabel(shape)
        self.canvas.loadShapes(shapes)

    def loadLabels(self, shapes):
        s = []
        for label, points, line_color, fill_color in shapes:
            shape = Shape(label=label, editable=self._config['shape_editable_default'])
            for x, y in points:
                shape.addPoint(QtCore.QPoint(x, y))
            shape.close()
            s.append(shape)
            if line_color:
                shape.line_color = QtGui.QColor(*line_color)
            if fill_color:
                shape.fill_color = QtGui.QColor(*fill_color)
        self.loadShapes(s)

    def loadFlags(self, flags):
        tmp_flags = dict()
        for key, flag in flags.items():
            if key.find('|') != -1:
                tmp_flags[key] = flag

        flags = tmp_flags
        self.flag_widget.clear()
        for key, flag in flags.items():
            item = QtWidgets.QListWidgetItem(key)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if flag else Qt.Unchecked)
            try:
                bcc = key.split('|')[-1]
                # item.setBackground(QtGui.QColor(bcc))
                item.setForeground(QtGui.QColor(bcc))
            except:
                pass
            self.flag_widget.addItem(item)
        self.flag_item_changed() # after load flag, show properly

    def loadSortFlags(self, flags):
        self.sort_file_widget.clear()
        for key, flag in flags.items():
            item = QtWidgets.QListWidgetItem(key)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if flag else Qt.Unchecked)
            self.sort_file_widget.addItem(item)
        self.sort_type_flag_item_changed() # after load flag, show properly

    def load_current_flag(self, flag_widget_chosen=None):
        if flag_widget_chosen is None:
            flag_widget_chosen = self.flag_widget
        flags = {}
        for i in range(flag_widget_chosen.count()):
            item = flag_widget_chosen.item(i)
            key = item.text()
            flag = item.checkState() == Qt.Checked
            flags[key] = flag
        return flags

    def saveLabels(self, filename):
        lf = LabelFile(_img_common_prefix_dir=self._config['img_common_prefix_dir'])

        def format_shape(s):
            return dict(label=str(s.label),
                        line_color=s.line_color.getRgb()
                        if s.line_color != self.lineColor else None,
                        fill_color=s.fill_color.getRgb()
                        if s.fill_color != self.fillColor else None,
                        points=[(p.x(), p.y()) for p in s.points])

        shapes = [format_shape(shape) for shape in self.labelList.shapes]
        for shape in self.labelList.shapes:
            shape.restore_editable()

        flags = self.load_current_flag(self.flag_widget)
        flags['verified'] = self.canvas.verified
        try:
            # imagePath = os.path.relpath(
            #     self.imagePath, os.path.dirname(filename))
            imagePath = self.imagePath
            imageData = self.imageData if self._config['store_data'] else None
            lf.save(
                filename=filename,
                shapes=shapes,
                imagePath=imagePath,
                imageData=imageData,
                lineColor=self.lineColor.getRgb(),
                fillColor=self.fillColor.getRgb(),
                otherData=self.otherData,
                flags=flags,
            )
            if self.labelFile == None:
                self.labelFile = lf #note here, when we already load it from file, this will throw otherData, so, do not do this
            # disable allows next and previous image to proceed
            # self.filename = filename
            return True
        except LabelFileError as e:
            self.errorMessage('Error saving label data', '<b>%s</b>' % e)
            return False

    def copySelectedShape(self):
        self.addLabel(self.canvas.copySelectedShape())
        # fix copy and delete
        self.shapeSelectionChanged(True)

    def labelSelectionChanged(self):
        item = self.currentItem()
        if item and self.canvas.editing():
            self._noSelectionSlot = True
            shape = self.labelList.get_shape_from_item(item)
            self.canvas.selectShape(shape)

    def labelItemChanged(self, item):
        shape = self.labelList.get_shape_from_item(item)
        label = str(item.text())
        if label != shape.label:
            shape.label = str(item.text())
            self.setDirty()
        else:  # User probably changed item visibility
            self.canvas.setShapeVisible(shape, item.checkState() == Qt.Checked)

    # Callback functions:

    def newShape(self):
        """Pop-up and give focus to the label editor.

        position MUST be in global coordinates.
        """
        items = self.uniqLabelList.selectedItems()
        text = None
        if items:
            text = items[0].text()
        text = self.labelDialog.popUp(text)

        try:
            label_, label_id_ = text.split('~')
            assert int(label_id_) > 100000
        except Exception as e:
            msg = QtWidgets.QMessageBox()
            msg.setText("In newShape, the label should have format LABELNAME~ID, and to make sure the id won't be same as existing ones, make it larger than 10000 please")
            msg.exec_()
            text = None

        if text is not None and not self.validateLabel(text):
            self.errorMessage('Invalid label',
                              "Invalid label '{}' with validation type '{}'"
                              .format(text, self._config['validate_label']))
            text = None
        if text is None:
            self.canvas.undoLastLine()
            self.canvas.shapesBackups.pop()
        else:
            self.addLabel(self.canvas.setLastLabel(text))
            self.actions.editMode.setEnabled(True)
            self.actions.editRectangleMode.setEnabled(True)
            self.actions.undoLastPoint.setEnabled(False)
            self.actions.undo.setEnabled(True)
            self.setDirty()

    def scrollRequest(self, delta, orientation):
        units = - delta * 0.1  # natural scroll
        bar = self.scrollBars[orientation]
        bar.setValue(bar.value() + bar.singleStep() * units)

    def setZoom(self, value):
        self.actions.fitWidth.setChecked(False)
        self.actions.fitWindow.setChecked(False)
        self.zoomMode = self.MANUAL_ZOOM
        self.zoomWidget.setValue(value)

    def addZoom(self, increment=10):
        self.setZoom(self.zoomWidget.value() + increment)

    def zoomRequest(self, delta, pos):
        canvas_width_old = self.canvas.width()

        units = delta * 0.1
        self.addZoom(units)

        canvas_width_new = self.canvas.width()
        if canvas_width_old != canvas_width_new:
            canvas_scale_factor = canvas_width_new / canvas_width_old

            x_shift = round(pos.x() * canvas_scale_factor) - pos.x()
            y_shift = round(pos.y() * canvas_scale_factor) - pos.y()

            self.scrollBars[Qt.Horizontal].setValue(
                self.scrollBars[Qt.Horizontal].value() + x_shift)
            self.scrollBars[Qt.Vertical].setValue(
                self.scrollBars[Qt.Vertical].value() + y_shift)

    def setFitWindow(self, value=True):
        if value:
            self.actions.fitWidth.setChecked(False)
        self.zoomMode = self.FIT_WINDOW if value else self.MANUAL_ZOOM
        self.adjustScale()

    def setFitWidth(self, value=True):
        if value:
            self.actions.fitWindow.setChecked(False)
        self.zoomMode = self.FIT_WIDTH if value else self.MANUAL_ZOOM
        self.adjustScale()

    def togglePolygons(self, value):
        for item, shape in self.labelList.itemsToShapes:
            item.setCheckState(Qt.Checked if value else Qt.Unchecked)

    def togglePolygons_type(self, value, shape_chosen):
        for item, shape in self.labelList.itemsToShapes:
            if shape == shape_chosen:
                item.setCheckState(Qt.Checked if value else Qt.Unchecked)


    def assign_labelfile_with_imagepath(self, filename, _force=True):
        if LabelFile.isLabelFile(filename):
            label_file = filename
        elif filename in self.image_path_labelme_file_dict: # use labelme file
            label_file = self.image_path_labelme_file_dict[filename]
        elif self.defaultSaveDir is not None:
            basename = os.path.basename(
                os.path.splitext(filename)[0])
            label_file = os.path.join(self.defaultSaveDir, basename + '.json')
        elif _force:
            label_file = os.path.splitext(filename)[0] + '.json'
        else:
            label_file = None
        return label_file

    def loadFile(self, filename=None):
        """Load the specified file, or the last opened file if None."""
        # changing fileListWidget loads file

        if (filename in self.imageList and
                self.fileListWidget.currentRow() !=
                self.imageList.index(filename)):
            self.fileListWidget.setCurrentRow(self.imageList.index(filename))
            return

        self.resetState()
        self.canvas.setEnabled(False)
        if filename is None:
            filename = self.settings.value('filename', '')
        filename = str(filename)
        """
        if not QtCore.QFile.exists(filename):
            self.errorMessage(
                'Error opening file', 'No such file: <b>%s</b>' % filename)
            return False
        """
        # assumes same name, but json extension
        self.status("Loading %s..." % os.path.basename(str(filename)))

        label_file = self.assign_labelfile_with_imagepath(filename)

        if QtCore.QFile.exists(label_file) and \
                LabelFile.isLabelFile(label_file):
            try:
                self.labelFile = LabelFile(label_file, _img_common_prefix_dir=self._config['img_common_prefix_dir'])
                # FIXME: PyQt4 installed via Anaconda fails to load JPEG
                # and JSON encoded images.
                # https://github.com/ContinuumIO/anaconda-issues/issues/131
                if QtGui.QImage.fromData(self.labelFile.imageData).isNull():
                    raise LabelFileError(
                        'Failed loading image data from label file.\n'
                        'Maybe this is a known issue of PyQt4 built on'
                        ' Anaconda, and may be fixed by installing PyQt5.')
            except LabelFileError as e:
                self.errorMessage(
                    'Error opening file',
                    "<p><b>%s</b></p>"
                    "<p>Make sure <i>%s</i> is a valid label file."
                    % (e, label_file))
                self.status("Error reading %s" % label_file)
                return False
            self.imageData = self.labelFile.imageData
            # self.imagePath = os.path.join(os.path.dirname(label_file),
            #                               self.labelFile.imagePath)
            # self.imagePath = os.path.abspath(self.labelFile.imagePath)
            self.imagePath = self.labelFile.imagePath
            self.lineColor = QtGui.QColor(*self.labelFile.lineColor)
            self.fillColor = QtGui.QColor(*self.labelFile.fillColor)
            self.otherData = self.labelFile.otherData
            self.canvas.verified = self.labelFile.flags.get("verified", False)
        else:
            # Load image:
            # read data first and store for saving into label file.
            self.imageData = read(filename, None)
            if self.imageData is not None:
                # the filename is image not JSON
                self.imagePath = os.path.abspath(filename)  # save with the absolute path
            self.labelFile = None
            self.canvas.verified = False
        image = QtGui.QImage.fromData(self.imageData)
        if image.isNull():
            formats = ['*.{}'.format(fmt.data().decode())
                       for fmt in QtGui.QImageReader.supportedImageFormats()]
            self.errorMessage(
                'Error opening file',
                '<p>Make sure <i>{0}</i> is a valid image file.<br/>'
                'Supported image formats: {1}</p>'
                .format(filename, ','.join(formats)))
            self.status("Error reading %s" % filename)
            return False
        self.image = image
        self.filename = filename
        if self._config['keep_prev']:
            prev_shapes = self.canvas.shapes
        self.canvas.loadPixmap(QtGui.QPixmap.fromImage(image))
        if self._config['flags']:
            self.loadFlags({k: False for k in self._config['flags']})
        if self._config['keep_prev']:
            self.loadShapes(prev_shapes)
        if self.labelFile:
            self.loadLabels(self.labelFile.shapes)
            if self.labelFile.flags is not None:
                self.loadFlags(self.labelFile.flags)
        self.setClean()
        self.canvas.setEnabled(True)
        self.adjustScale(initial=True)
        self.paintCanvas()
        self.addRecentFile(self.filename)
        self.toggleActions(True)
        self.status("Loaded %s" % os.path.basename(str(filename)))
        return True

    def resizeEvent(self, event):
        if self.canvas and not self.image.isNull()\
           and self.zoomMode != self.MANUAL_ZOOM:
            self.adjustScale()
        super(MainWindow, self).resizeEvent(event)

    def paintCanvas(self):
        assert not self.image.isNull(), "cannot paint null image"
        self.canvas.scale = 0.01 * self.zoomWidget.value()
        self.canvas.adjustSize()
        self.canvas.update()

    def adjustScale(self, initial=False):
        value = self.scalers[self.FIT_WINDOW if initial else self.zoomMode]()
        self.zoomWidget.setValue(int(100 * value))

    def scaleFitWindow(self):
        """Figure out the size of the pixmap to fit the main widget."""
        e = 2.0  # So that no scrollbars are generated.
        w1 = self.centralWidget().width() - e
        h1 = self.centralWidget().height() - e
        a1 = w1 / h1
        # Calculate a new scale value based on the pixmap's aspect ratio.
        w2 = self.canvas.pixmap.width() - 0.0
        h2 = self.canvas.pixmap.height() - 0.0
        a2 = w2 / h2
        return w1 / w2 if a2 >= a1 else h1 / h2

    def scaleFitWidth(self):
        # The epsilon does not seem to work too well here.
        w = self.centralWidget().width() - 2.0
        return w / self.canvas.pixmap.width()

    def closeEvent(self, event):
        if not self.mayContinue():
            event.ignore()
        self.settings.setValue(
            'filename', self.filename if self.filename else '')
        self.settings.setValue('window/size', self.size())
        self.settings.setValue('window/position', self.pos())
        self.settings.setValue('window/state', self.saveState())
        self.settings.setValue('line/color', self.lineColor)
        self.settings.setValue('fill/color', self.fillColor)
        self.settings.setValue('recentFiles', self.recentFiles)
        # ask the use for where to save the labels
        # self.settings.setValue('window/geometry', self.saveGeometry())

    # User Dialogs #

    def loadRecent(self, filename):
        if self.mayContinue():
            self.loadFile(filename)

    def verifyImg(self, _value=False):
        # Proceding next image without dialog if having any label
         if self.filename is not None:
            try:
                self.labelFile.toggleVerify()
            except AttributeError:
                # If the labelling file does not exist yet, create if and
                # re-save it with the verified attribute.
                self.saveFile(_force=True) # will always save file, even we has no label (if the user really think it is okay to have no label, then just save)
                if self.labelFile != None:
                    self.labelFile.toggleVerify()
                else:
                    return

            self.canvas.verified = self.labelFile.verified
            self.paintCanvas()
            self.saveFile(_force=True)

    def openPrevImg(self, _value=False):
        if not self.mayContinue():
            return

        if len(self.imageList) <= 0:
            return

        if self.filename is None:
            return

        currIndex = self.imageList.index(self.filename)
        if currIndex - 1 >= 0:
            filename = self.imageList[currIndex - 1]
            if filename:
                self.loadFile(filename)
        else:
            self.errorMessage('Reach first image',
                              'This is already the first image!')
            return

    def openNextImg(self, _value=False, load=True):
        if not self.mayContinue():
            return

        if len(self.imageList) <= 0:
            return

        if self.filename is None:
            filename = self.imageList[0]
        else:
            currIndex = self.imageList.index(self.filename)
            if currIndex + 1 < len(self.imageList):
                filename = self.imageList[currIndex + 1]
            else:
                self.errorMessage('Reach last image',
                                  'This is already the last image!')
                return
        self.filename = filename

        if self.filename and load:
            self.loadFile(self.filename)

    def openFile(self, _value=False):
        if not self.mayContinue():
            return
        path = os.path.dirname(str(self.filename)) if self.filename else '.'
        formats = ['*.{}'.format(fmt.data().decode())
                   for fmt in QtGui.QImageReader.supportedImageFormats()]
        filters = "Image & Label files (%s)" % ' '.join(
            formats + ['*%s' % LabelFile.suffix])
        filename = QtWidgets.QFileDialog.getOpenFileName(
            self, '%s - Choose Image or Label file' % __appname__,
            path, filters)
        if QT5:
            filename, _ = filename
        filename = str(filename)
        if filename:
            self.loadFile(filename)

    def saveFile(self, _force=False):
        assert not self.image.isNull(), "cannot save empty image"
        if self._config['flags'] or self.hasLabels() or _force:
            if self.labelFile:
                # DL20180323 - overwrite when in directory
                self._saveFile(self.labelFile.filename)
                return
            if self.output:
                self._saveFile(self.output)
                return
            label_file = self.assign_labelfile_with_imagepath(self.filename, _force=False)
            if label_file is not None:
                self._saveFile(label_file)
                return
            self._saveFile(self.saveFileDialog())

    def saveFileAs(self, _value=False):
        assert not self.image.isNull(), "cannot save empty image"
        if self.hasLabels():
            self._saveFile(self.saveFileDialog())

    def saveFileDialog(self):
        caption = '%s - Choose File' % __appname__
        filters = 'Label files (*%s)' % LabelFile.suffix
        dlg = QtWidgets.QFileDialog(self, caption, self.currentPath(), filters)
        dlg.setDefaultSuffix(LabelFile.suffix[1:])
        dlg.setAcceptMode(QtWidgets.QFileDialog.AcceptSave)
        dlg.setOption(QtWidgets.QFileDialog.DontConfirmOverwrite, False)
        dlg.setOption(QtWidgets.QFileDialog.DontUseNativeDialog, False)
        basename = os.path.splitext(self.filename)[0]
        default_labelfile_name = os.path.join(
            self.currentPath(), basename + LabelFile.suffix)
        filename = dlg.getSaveFileName(
            self, 'Choose File', default_labelfile_name,
            'Label files (*%s)' % LabelFile.suffix)
        if QT5:
            filename, _ = filename
        filename = str(filename)
        return filename

    def _saveFile(self, filename):
        if filename and self.saveLabels(filename):
            self.addRecentFile(filename)
            self.setClean()
            if self.labeling_once:
                self.close()
            self.statusBar().showMessage('Saved to  %s' % filename)
            self.statusBar().show()

    def closeFile(self, _value=False):
        if not self.mayContinue():
            return
        self.resetState()
        self.setClean()
        self.toggleActions(False)
        self.canvas.setEnabled(False)
        self.actions.saveAs.setEnabled(False)

    # Message Dialogs. #
    def hasLabels(self):
        if not self.labelList.itemsToShapes:
            self.errorMessage(
                'No objects labeled',
                'You must label at least one object to save the file.')
            return False
        return True

    def mayContinue(self):
        if not self.dirty:
            return True
        mb = QtWidgets.QMessageBox
        msg = 'Save annotations to "{}" before closing?'.format(self.filename)
        answer = mb.question(self,
                             'Save annotations?',
                             msg,
                             mb.Save | mb.Discard | mb.Cancel,
                             mb.Save)
        if answer == mb.Save:
            self.saveFile()
            return True
        elif answer == mb.Discard:
            self.dirty = False
            return True
        else:  # answer == mb.Cancel
            return False

    def errorMessage(self, title, message):
        return QtWidgets.QMessageBox.critical(
            self, title, '<p><b>%s</b></p>%s' % (title, message))
        pass
    def warnMessage(self, title, message):
        return QtWidgets.QMessageBox.warning(self, title, '<p><b>%s</b></p>%s' % (title, message))
        pass
    def confirmMessage(self, title, message):
        rtn = QtWidgets.QMessageBox.information(self,
                                                title,
                                                message,
                                                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
        if rtn == QtWidgets.QMessageBox.Yes:
            return True
        else:
            return False

    def currentPath(self):
        return os.path.dirname(str(self.filename)) if self.filename else '.'

    def chooseColor1(self):
        color = self.colorDialog.getColor(
            self.lineColor, 'Choose line color', default=DEFAULT_LINE_COLOR)
        if color:
            self.lineColor = color
            # Change the color for all shape lines:
            Shape.line_color = self.lineColor
            self.canvas.update()
            self.setDirty()

    def chooseColor2(self):
        color = self.colorDialog.getColor(
            self.fillColor, 'Choose fill color', default=DEFAULT_FILL_COLOR)
        if color:
            self.fillColor = color
            Shape.fill_color = self.fillColor
            self.canvas.update()
            self.setDirty()

    def deleteSelectedShape(self):
        yes, no = QtWidgets.QMessageBox.Yes, QtWidgets.QMessageBox.No
        msg = 'You are about to permanently delete this polygon, ' \
              'proceed anyway?'
        if yes == QtWidgets.QMessageBox.warning(self, 'Attention', msg,
                                                yes | no):
            self.remLabel(self.canvas.deleteSelected())
            self.setDirty()
            if self.noShapes():
                for action in self.actions.onShapesPresent:
                    action.setEnabled(False)

    def chshapeLineColor(self):
        color = self.colorDialog.getColor(
            self.lineColor, 'Choose line color', default=DEFAULT_LINE_COLOR)
        if color:
            self.canvas.selectedShape.line_color = color
            self.canvas.update()
            self.setDirty()

    def chshapeFillColor(self):
        color = self.colorDialog.getColor(
            self.fillColor, 'Choose fill color', default=DEFAULT_FILL_COLOR)
        if color:
            self.canvas.selectedShape.fill_color = color
            self.canvas.update()
            self.setDirty()

    def copyShape(self):
        self.canvas.endMove(copy=True)
        self.addLabel(self.canvas.selectedShape)
        self.setDirty()

    def moveShape(self):
        self.canvas.endMove(copy=False)
        self.setDirty()

    def changeSavedirDialog(self, _value=False):
        if self.defaultSaveDir is not None:
            path = self.defaultSaveDir
        else:
            path = '.'

        dirpath = QtWidgets.QFileDialog.getExistingDirectory(self,
                                                       '%s - Save annotations to the directory' % __appname__, path,  QtWidgets.QFileDialog.ShowDirsOnly
                                                       | QtWidgets.QFileDialog.DontResolveSymlinks)

        if dirpath is not None and len(dirpath) > 1:
            self.defaultSaveDir = dirpath

        self.statusBar().showMessage('%s . Annotation will be saved to %s' %
                                     ('Change saved folder', self.defaultSaveDir))
        self.statusBar().show()

    def openDirDialog(self, _value=False, dirpath=None):
        if not self.mayContinue():
            return

        if _value == True:
            assert os.path.exists(dirpath)
            self.targetDirPath = dirpath
            self.importDirImages(self.targetDirPath)
            return 

        defaultOpenDirPath = dirpath if dirpath else '.'
        if self.lastOpenDir and os.path.exists(self.lastOpenDir):
            defaultOpenDirPath = self.lastOpenDir
        else:
            defaultOpenDirPath = os.path.dirname(self.filename) \
                if self.filename else '.'

        self.targetDirPath = str(QtWidgets.QFileDialog.getExistingDirectory(
            self, '%s - Open Directory' % __appname__, defaultOpenDirPath,
            QtWidgets.QFileDialog.ShowDirsOnly |
            QtWidgets.QFileDialog.DontResolveSymlinks))
        self.importDirImages(self.targetDirPath)

    @property
    def imageList(self):
        lst = []
        for i in range(self.fileListWidget.count()):
            item = self.fileListWidget.item(i)
            lst.append(item.text())
        return lst

    def _custom_key(self, d, key_s='A'):
        return d[1].get(key_s, 0.0)

    def change_image_path_list_order(self, order_by_type='SHOW_TYPE_TP'):

        d_s = sorted(self.image_path_stats_dict_list_backup.items(), key=lambda x: self._custom_key(x, key_s=order_by_type), reverse=True) # okay, the bigger the earlier
        # print(d_s)
        image_path_list = [x[0] for x in d_s]
        # print(image_path_list)

        return image_path_list

    def import_changed_image_path_list(self, image_path_list, load=True):

        if len(image_path_list) == 0:
            return

        self.filename = None
        self.fileListWidget.clear()

        for imgPath in image_path_list:
            item = QtWidgets.QListWidgetItem(imgPath)
            self.fileListWidget.addItem(item)
        self.openNextImg(load=load)


    def importDirImages(self, dirpath, load=True):
        if not self.mayContinue() or not dirpath:
            return

        self.lastOpenDir = dirpath
        self.filename = None
        self.fileListWidget.clear()
        self.image_path_stats_dict_list_backup = dict()
        self.image_path_list_last = list()
        self.image_path_labelme_file_dict = dict()

        allImagesInDir = self.scanAllImages(dirpath)
        allJsonLabelsInDir = self.scanAllJsonLables(dirpath)


        if len(allImagesInDir) == 0 and len(allJsonLabelsInDir) == 0:
            self.warnMessage('No images or labels found',
                              "")
            return
        if len(allImagesInDir) > 0 and len(allJsonLabelsInDir) > 0:
            self.errorMessage('Both images or labels found',
                             "We do not allow images and labels being in same directory, put them in different places please")
            return

        if len(allImagesInDir) > 0 and len(allJsonLabelsInDir) == 0:
            self.warnMessage('Use images',
                             "Note you are using images only, and no json files are in the chosen directory (which is good)")

        if len(allImagesInDir) == 0 and len(allJsonLabelsInDir) > 0:
            allImagesInDir = []
            for json_f in allJsonLabelsInDir:
                labelFile = LabelFile(json_f, _load_data=False, _img_common_prefix_dir=self._config['img_common_prefix_dir'])
                imagePath = labelFile.imagePath
                allImagesInDir.append(str(imagePath))
                self.image_path_list_last.append(imagePath)
                self.image_path_labelme_file_dict[imagePath] = json_f
                self.image_path_stats_dict_list_backup[imagePath] = labelFile.otherData.get('custom_data', dict()).get('area_stats', dict())
            self.resetState()
            self.defaultSaveDir = dirpath
            self.warnMessage('Use labels',
                              "Note you are using labels only, and no images are in the chosen directory")

        for imgPath in allImagesInDir:
            item = QtWidgets.QListWidgetItem(imgPath)
            self.fileListWidget.addItem(item)
        self.openNextImg(load=load)

    def scanAllImages(self, folderPath):
        extensions = ['.%s' % fmt.data().decode("ascii").lower()
                      for fmt in QtGui.QImageReader.supportedImageFormats()]
        images = []

        for root, dirs, files in os.walk(folderPath):
            for file in files:
                if file.lower().endswith(tuple(extensions)):
                    relativePath = os.path.join(root, file)
                    images.append(relativePath)
        images.sort(key=lambda x: x.lower())
        return images

    def scanAllJsonLables(self, folderPath):
        extensions = ['.json']
        json_files = []

        for root, dirs, files in os.walk(folderPath):
            for file in files:
                if file.lower().endswith(tuple(extensions)):
                    relativePath = os.path.join(root, file)
                    json_files.append(relativePath)
        json_files.sort(key=lambda x: x.lower())
        return json_files


def inverted(color):
    return QtGui.QColor(*[255 - v for v in color.getRgb()])


def read(filename, default=None):
    try:
        if os.path.isfile(filename):
            with open(filename, 'rb') as f:
                return f.read()
        elif filename.startswith('http'):
            resp = urlopen(filename.replace('\\', '//'))
            return resp.read()
    except Exception:
        return default

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', '-V', action='store_true',
                        help='show version')
    parser.add_argument('filename', nargs='?', help='image or label filename')
    parser.add_argument('--output', '-O', '-o', help='output label name')
    default_config_file = os.path.join(os.path.expanduser('~'), '.labelmerc')
    parser.add_argument(
        '--config',
        dest='config_file',
        help='config file (default: %s)' % default_config_file,
        default=default_config_file,
    )
    # config for the gui
    parser.add_argument(
        '--nodata',
        dest='store_data',
        action='store_false',
        help='stop storing image data to JSON file',
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--autosave',
        dest='auto_save',
        action='store_true',
        help='auto save',
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--nosortlabels',
        dest='sort_labels',
        action='store_false',
        help='stop sorting labels',
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--flags',
        help='comma separated list of flags OR file containing flags',
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--labels',
        help='comma separated list of labels OR file containing labels',
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--validatelabel',
        dest='validate_label',
        choices=['exact', 'instance'],
        help='label validation types',
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--keep-prev',
        action='store_true',
        help='keep annotation of previous frame',
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--change_labelme_attr_with_gui_file_path',
        help='The implement of change lablem attr with gui file',
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--label_type',
        help='The labeltype we are dealing with',
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--image_dir',
        help='The image directory, you can use with the save_labelme_dir, or use as one fresh start',
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--save_labelme_dir',
        help='The saving directory of labelme files (use with iamge_dir) or you can just load with these files (no need to specify the image dir manually)',
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--shape_editable_default',
        action='store_true',
        help='Whether the shape is editable or not by default',
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--img_common_prefix_dir',
        help='The common prefix dir of these files',
        default=argparse.SUPPRESS,
    )

    args = parser.parse_args()

    if args.version:
        print('{0} {1}'.format(__appname__, __version__))
        sys.exit(0)

    if hasattr(args, 'flags'):
        if os.path.isfile(args.flags):
            with codecs.open(args.flags, 'r', encoding='utf-8') as f:
                args.flags = [l.strip() for l in f if l.strip()]
        else:
            args.flags = [l for l in args.flags.split(',') if l]

    if hasattr(args, 'labels'):
        if os.path.isfile(args.labels):
            with codecs.open(args.labels, 'r', encoding='utf-8') as f:
                args.labels = [l.strip() for l in f if l.strip()]
        else:
            args.labels = [l for l in args.labels.split(',') if l]

    config_from_args = args.__dict__
    config_from_args.pop('version')
    filename = config_from_args.pop('filename')
    output = config_from_args.pop('output')
    config_file = config_from_args.pop('config_file')
    config = get_config(config_from_args, config_file)

    if not config['labels'] and config['validate_label']:
        logger.error('--labels must be specified with --validatelabel or '
                     'validate_label: true in the config file '
                     '(ex. ~/.labelmerc).')
        sys.exit(1)

    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(__appname__)
    app.setWindowIcon(newIcon('icon'))
    win = MainWindow(config=config, filename=filename, output=output)

    # preprocess
    win.setEditRectangleMode()

    win.show()
    win.raise_()
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
