import contextlib
import os
import urllib
import urllib2

import docutils.nodes
import docutils.utils

from urllib import quote as urlquote, pathname2url
from urlparse import urlparse, urlunparse, urljoin

from docutils.parsers import rst
from sphinx.util.nodes import split_explicit_title
from sphinx.errors import ExtensionError

from loader import ClassLoader
from model import parse_name

def setup(app):
    app.add_config_value('javalink_classpath', [], '')
    app.add_config_value('javalink_docroots', [], '')

    app.add_config_value('javalink_add_package_names', True, '')
    app.add_config_value('javalink_qualify_nested_types', True, '')
    app.add_config_value('javalink_add_method_parameters', True, '')

    app.add_directive('javaimport', JavadocImportDirective)
    app.add_role('javaref', JavaRefRole(app))

    # TODO is this the right time to do this?
    app.connect('builder-inited', initialize_package_list)
    app.connect('env-purge-doc', purge_imports)


class JavalinkEnvAccessor:
    @property
    def env(self):
        pass

    @property
    def classloader(self):
        if not hasattr(self.env, 'javalink_classloader'):
            classpath = self.env.config.javalink_classpath
            self.env.javalink_classloader = ClassLoader(classpath)

        return self.env.javalink_classloader

    @property
    def imports(self):
        if not hasattr(self.env, 'javalink_imports'):
            self.env.javalink_imports = {}

        return self.env.javalink_imports


class JavadocImportDirective(rst.Directive, JavalinkEnvAccessor):
    required_arguments = 0
    optional_arguments = 0
    has_content = True

    @property
    def env(self):
        return self.state.document.settings.env

    def run(self):
        docname = self.env.docname

        if not docname in self.imports:
            self.imports[docname] = [('java.lang', '*')]

        imports = []
        for i in self.content:
            package, name = parse_name(i.strip())
            # TODO make this work with Package objects
            self._validate_import(package.name, name)
            imports.append((package.name, name))

        self.imports[docname].extend(imports)
        return []

    # TODO handle conflicting imports
    def _validate_import(self, package, name):
        if name == '*':
            entity = self.classloader.find_package(package)
        else:
            entity = self.classloader.load('{}.{}'.format(package, name))

        if not entity:
            self.error("unresolved import '{}.{}'".format(package, name))


class JavaRefRole(JavalinkEnvAccessor):
    def __init__(self, app):
        self.app = app

    @property
    def env(self):
        return self.app.env

    def find_ref(self, reftext):
        reftext = reftext.strip()

        # TODO add additional validation (see SeeTagImpl.java)
        where, _, what = reftext.partition('#')

        clazz = self._find_class(where)
        if clazz:
            where = clazz.full_name
            if what:
                # TODO handle member ambiguity
                member = clazz.get_member(what)
                if not member:
                    raise JavaRefError('unknown member: {}'.format(reftext))

                what = member.get_url_fragment()

            return where, what

        if not what:
            package = self.classloader.find_package(where)
            if package:
                return package.name + '.package-summary', None

        raise JavaRefError('reference not found: {}'.format(reftext))

    def to_url(self, where, what):
        root = self._find_url_root(where)
        if not root:
            raise JavaRefError('root URL not found: {}'.format(where))

        path = where.replace('.', '/').replace('$', '.')
        path += '.html'
        if what:
            # TODO is this the correct way to escape the URL?
            path += '#{}'.format(urlquote(what, ';/?:@&=+$,()'))

        return urljoin(root, path)

    def to_title(self, where, what):
        package, name = parse_name(where)
        if name == 'package-summary':
            return package.name

        add_package_names = self.app.config.javalink_add_package_names
        qualify_nested_types = self.app.config.javalink_qualify_nested_types
        add_method_parameters = self.app.config.javalink_add_method_parameters

        title = []
        if add_package_names:
            title.append(package.name)

        if add_package_names or qualify_nested_types:
            title.append(name.replace('$', '.'))
        else:
            title.append(name.rpartition('$')[-1])

        if what:
            if add_method_parameters:
                title.append(what)
            else:
                title.append(what.partition('(')[0])

        return '.'.join(title)

    def _find_class(self, where):
        import_name = where.partition('.')[0]
        imports = self.imports.get(self.env.docname, [])

        candidates = [where]
        for package, name in imports:
            if name == import_name or name == '*':
                candidates.append('{}.{}'.format(package, where))

        for name in candidates:
            clazz = self.classloader.load(name)
            if clazz:
                return clazz

            # try all possible class nestings
            sep = name.rfind('.')
            while sep >= 0:
                name = name[:sep] + '$' + name[sep+1:]
                clazz = self.classloader.load(name)
                if clazz:
                    return clazz

                sep = name.rfind('.', 0, sep)

        return None

    def _find_url_root(self, where):
        package, _ = parse_name(where)
        return self.env.javalink_packages.get(package.name, None)

    def __call__(self, name, rawtext, text, lineno, inliner,
                 options={}, content=[]):

        text = docutils.utils.unescape(text)
        has_title, title, reftext = split_explicit_title(text)
        if not has_title:
            title = title.replace('#', '.')
            # if not self.app.config.add_function_parentheses:

        warnings = []
        try:
            where, what = self.find_ref(reftext)
            url = self.to_url(where, what)
            if not has_title:
                title = self.to_title(where, what)
        except JavaRefError as e:
            url = None
            warnings.append(e.reason)

        if url:
            # if no scheme, assume a local path relative to the src root
            if not urlparse(url).scheme:
                docdir = os.path.dirname(inliner.document.current_source)
                url = os.path.relpath(self.env.srcdir, docdir) + '/' + url

            ref = docutils.nodes.reference('', '', internal=False, refuri=url)
            ref.append(docutils.nodes.literal(rawsource=title, text=title))
        else:
            ref = docutils.nodes.literal(rawsource=title, text=title)

        return [ref], [inliner.reporter.warning(w, line=lineno) for w in warnings]


class JavaRefError(Exception):
    """Raised when a reference to a Java element cannot be resolved.

    Attributes:
        reason -- reason why the reference is unresolved
    """

    def __init__(self, reason):
        self.reason = reason

    def __str__(self):
        return str(self.reason)


def find_rt_jar(javahome=None):
    if not javahome:
        if 'JAVA_HOME' in os.environ:
            javahome = os.environ['JAVA_HOME']
        else:
            javahome = _get_javahome_from_java(_find_java_binary())

    rtpath = os.path.join(javahome, 'jre', 'lib', 'rt.jar')
    if not os.path.isfile(rtpath):
        msg = 'Could not find rt.jar: {} is not a file'.format(rtpath)
        raise ExtensionError(msg)

    return rtpath


def _get_javahome_from_java(java):
    while os.path.islink(java):
        link = java
        java = os.readlink(link)
        if not os.path.isabs(java):
            java = os.path.join(os.path.dirname(link), java)

    javahome = os.path.join(os.path.dirname(java), '..', '..')
    return os.path.normpath(javahome)


def _find_java_binary():
    path = os.environ.get('PATH', os.defpath)

    exts = os.environ.get('PATHEXT', '')
    java_names = ['java' + ext for ext in exts.split(os.pathsep)]

    for p in path.split(os.pathsep):
        p = p.strip('"')
        for name in java_names:
            java = os.path.join(p, name)
            if os.path.isfile(java) and os.access(java, os.X_OK):
                return java

    raise ExtensionError("Could not find 'java' binary in PATH")


def purge_imports(app, env, docname):
    if hasattr(env, 'javalink_imports'):
        env.javalink_imports.pop(docname, None)


def initialize_package_list(app):
    env = app.env
    app.info('Initializing package list...')

    if not hasattr(env, 'javalink_packages'):
        env.javalink_packages = {}

    for url, base in [parse_docroot(r) for r in app.config.javalink_docroots]:
        try:
            with contextlib.closing(urllib2.urlopen(url)) as package_list:
                app.verbose('Retrieved package-list at %s', url)
                for package in package_list:
                    package = package.strip()
                    if package not in env.javalink_packages:
                        env.javalink_packages[package] = base
                    else:
                        app.warn("Duplicate package '{}' in {}".format(package, url))

        except urllib2.URLError as e:
            app.warn('Could not get {}; some links may not resolve'.format(url))
            app.verbose('Error was %s', e)


def parse_docroot(root):
    """
    Creates a package-list URL and a link base from a docroot
    element.
    """

    if isinstance(root, basestring):
        return _parse_docroot_str(root)
    else:
        url = _parse_docroot_str(root[0])[0]
        base = _parse_docroot_str(root[1])[1]
        return (url, base)


def _parse_docroot_str(root):
    """
    Creates a package-list URL and a link base from a docroot path or
    URL string.
    """

    scheme, netloc, path = urlparse(root)[0:3]
    if not scheme:
        # assume local path; add trailing '/'s if missing
        root = os.path.join(root, '')
        absroot = os.path.join(os.path.abspath(root), '')

        url = urljoin('file:///', pathname2url(absroot))
        base = pathname2url(root)
    else:
        path = path.rstrip('/') + '/'

        url = urlunparse((scheme, netloc, path, '', '', ''))
        base = url

    return (urljoin(url, 'package-list'), base)
