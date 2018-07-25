import os
from labelme.labelFile import LabelFile

class change_labelme_attr_with_gui_interface():
    def __init__(self, label_file_hook, label_type='person'):
        assert isinstance(label_file_hook, LabelFile)
        self.label_file_hook = label_file_hook # this is one hook <-> reference not copy
        self.label_type = label_type
        print("This is change_labelme_attr_with_gui_interface")

    def GUI(self, cur_id=0):
        pass

def load_change_labelme_attr_with_gui(file_path=None):
    if os.path.isfile(file_path):
        import importlib.util  # import file
        spec = importlib.util.spec_from_file_location("change_labelme_attr_with_gui", file_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.change_labelme_attr_with_gui
    else:
        return change_labelme_attr_with_gui_interface