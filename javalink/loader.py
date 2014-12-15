import itertools
import re

from javatools import jarinfo

def is_linkable_method(method_info):
    return not (method_info.is_bridge() or
                method_info.is_synthetic() or
                method_info.get_name() == '<clinit>')


def is_linkable_class(class_info):
    return class_info.is_public()


class LinkableClass:
    def __init__(self, class_info):
        fqn = class_info.get_this().replace('/', '.')
        self.package, self.name = fqn.rsplit('.', 1)

        self.fields = [f.get_name() for f in class_info.fields]

        self.methods = []
        for m in filter(is_linkable_method, class_info.methods):
            self.methods.append(LinkableMethod(self.name, m))

    def has_member(self, member):
        if member in self.fields:
            return True

        match = re.match(r'^(.+?)(?:\((.*)\))?$', member)
        if match:
            name, args = match.group(1, 2)

            method = next((m for m in self.methods if name == m.name), None)
            if method:
                if args is not None:
                    return method.has_args([a.strip() for a in args.split(',')])
                else:
                    return True

        return False


class LinkableMethod:
    def __init__(self, class_name, method):
        name = method.get_name()
        if name == '<init>':
            self.name = class_name.split('$')[-1]
        else:
            self.name = name

        self.args = list(method.pretty_arg_types())

    def has_args(self, args):
        # TODO enable fuzy matching for arguments
        return args == self.args


def load_jar(path, packages={}):
    jar = jarinfo.JarInfo(path)

    # use generators to parse jar entries lazily
    classes = (jar.get_classinfo(c) for c in jar.get_classes())
    for c in itertools.ifilter(is_linkable_class, classes):
        link_class = LinkableClass(c)
        if link_class.package not in packages:
            packages[link_class.package] = {}
        packages[link_class.package][link_class.name] = link_class

    return packages


class ClassLoader:
    def __init__(self, paths):
        self.paths = list(paths)

    def load(self):
        self.packages = {}
        for path in self.paths:
            load_jar(path, self.packages)

    def has_target(self, package, clazz=None, member=None):
        if package not in self.packages:
            return False

        if clazz is not None:
            if clazz not in self.packages[package]:
                return False
            if member is not None:
                info = self.packages[package][clazz]
                return info.has_member(member)

        return True
