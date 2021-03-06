#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (c) 2013, Sascha Peilicke <sascha@peilicke.de>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import

__doc__ = 'Generate distribution packages from PyPI'
__docformat__ = 'restructuredtext en'
__author__ = 'Sascha Peilicke <sascha@peilicke.de>'
__version__ = '0.6.3'

import argparse
import datetime
import glob
import os
import pickle
import pkg_resources
import pprint
import pwd
import re
import sys
import urllib
from six.moves import xmlrpc_client
import jinja2
import warnings
warnings.simplefilter('always', DeprecationWarning)

import py2pack.proxy
import py2pack.requires
import py2pack.utils


TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')
pypi = xmlrpc_client.ServerProxy('https://pypi.python.org/pypi')

SPDX_LICENSES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'spdx_license_map.p')
SDPX_LICENSES = pickle.load(open(SPDX_LICENSES_FILE, 'rb'))


def list(args=None):
    print('listing all PyPI packages...')
    for package in pypi.list_packages():
        print(package)


def search(args):
    print('searching for package {0}...'.format(args.name))
    for hit in pypi.search({'name': args.name}):
        print('found {0}-{1}'.format(hit['name'], hit['version']))


def show(args):
    check_or_set_version(args)
    print('showing package {0}...'.format(args.name))
    data = pypi.release_data(args.name, args.version)
    pprint.pprint(data)


def fetch(args):
    check_or_set_version(args)
    url = newest_download_url(args)
    if not url:
        print("unable to find a source release for {0}!".format(args.name))
        sys.exit(1)
    print('downloading package {0}-{1}...'.format(args.name, args.version))
    print('from {0}'.format(url['url']))
    urllib.urlretrieve(url['url'], url['filename'])


def _run_setup_py(tarfile, data):
    with py2pack.utils._extract_to_tempdir(tarfile) as (tmp_dir, names):
        d = py2pack.requires._requires_from_setup_py_run(tmp_dir)
    data.update(d)
    return names


def _canonicalize_setup_data(data):
    if "install_requires" in data:
        # install_requires may be a string, convert to list of strings:
        if isinstance(data["install_requires"], str):
            data["install_requires"] = data["install_requires"].splitlines()
        data["install_requires"] = \
            py2pack.requires._requirements_sanitize(data["install_requires"])

    if "tests_require" in data:
        # tests_require may be a string, convert to list of strings:
        if isinstance(data["tests_require"], str):
            data["tests_require"] = data["tests_require"].splitlines()
        data["tests_require"] = \
            py2pack.requires._requirements_sanitize(data["tests_require"])

    if "extras_require" in data:
        # extras_require value may be a string, convert to list of strings:
        for (key, value) in data["extras_require"].items():
            if isinstance(value, str):
                data["extras_require"][key] = value.splitlines()
            data["extras_require"][key] = \
                py2pack.requires._requirements_sanitize(data["extras_require"][key])

    if "data_files" in data:
        # data_files may be a sequence of files without a target directory:
        if len(data["data_files"]) and isinstance(data["data_files"][0], str):
            data["data_files"] = [("", data["data_files"])]
        # directory paths may be relative to the installation prefix:
        prefix = sys.exec_prefix if "is_extension" in data else sys.prefix
        data["data_files"] = [
            (dir if (len(dir) and dir[0] == '/') else os.path.join(prefix, dir), files)
            for (dir, files) in data["data_files"]]

    if "entry_points" in data:
        # entry_points may be a string with .ini-style sections or a dict.
        # convert to a dict and parse it
        data["entry_points"] = pkg_resources.EntryPoint.parse_map(data["entry_points"])
        if "console_scripts" in data["entry_points"]:
            data["console_scripts"] = data["entry_points"]["console_scripts"].keys()


def _augment_data_from_tarball(args, filename, data):
    docs_re = re.compile("{0}-{1}\/((?:AUTHOR|ChangeLog|CHANGES|COPYING|LICENSE|NEWS|README).*)".format(args.name, args.version), re.IGNORECASE)
    shell_metachars_re = re.compile("[|&;()<>\s]")

    names = _run_setup_py(filename, data)

    _canonicalize_setup_data(data)

    for name in names:
        match = re.match(docs_re, name)
        if match:
            if "doc_files" not in data:
                data["doc_files"] = []
            if re.search(shell_metachars_re, match.group(1)):               # quote filename if it contains shell metacharacters
                data["doc_files"].append("'" + match.group(1) + "'")
            else:
                data["doc_files"].append(match.group(1))
        # Very broad check for testsuites
        if "test" in name.lower():
            data["testsuite"] = True


def _license_from_classifiers(data):
    """try to get a license from the classifiers"""
    classifiers = data.get('classifiers', [])
    found_license = None
    for c in classifiers:
        if c.startswith("License :: OSI Approved :: "):
            found_license = c.replace("License :: OSI Approved :: ", "")
    return found_license


def _normalize_license(data):
    """try to get SDPX license"""
    l = data.get('license', None)
    if not l:
        # try to get license from classifiers
        l = _license_from_classifiers(data)
    if l and l in SDPX_LICENSES.keys():
        data['license'] = SDPX_LICENSES[l]
    else:
        data['license'] = ""


def _prepare_template_env(template_dir):
    # setup jinja2 environment with custom filters
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(template_dir))
    env.filters['parenthesize_version'] = \
        lambda s: re.sub('([=<>]+)(.+)', r' (\1 \2)', s)
    env.filters['basename'] = \
        lambda s: s[s.rfind('/') + 1:]
    return env


def generate(args):
    # TODO (toabctl): remove this is a later release
    if args.run:
        warnings.warn("the '--run' switch is deprecated and a noop",
                      DeprecationWarning)

    check_or_set_version(args)
    if not args.template:
        args.template = file_template_list()[0]
    if not args.filename:
        args.filename = args.name + '.' + args.template.rsplit('.', 1)[1]   # take template file ending
    print('generating spec file for {0}...'.format(args.name))
    data = pypi.release_data(args.name, args.version)                       # fetch all meta data
    url = newest_download_url(args)
    if url:
        data['source_url'] = url['url']
    else:
        data['source_url'] = args.name + '-' + args.version + '.zip'
    data['year'] = datetime.datetime.now().year                             # set current year
    data['user_name'] = pwd.getpwuid(os.getuid())[4]                        # set system user (packager)
    data['summary_no_ending_dot'] = re.sub('(.*)\.', '\g<1>', data.get('summary', ""))

    tarball_file = glob.glob("{0}-{1}.*".format(args.name, args.version))   # we have a local tarball, try to
    if tarball_file:                                                        # get some more info from that
        _augment_data_from_tarball(args, tarball_file[0], data)

    _normalize_license(data)

    env = _prepare_template_env(TEMPLATE_DIR)
    template = env.get_template(args.template)
    result = template.render(data).encode('utf-8')                          # render template and encode properly
    outfile = open(args.filename, 'wb')                                     # write result to spec file
    try:
        outfile.write(result)
    finally:
        outfile.close()


def check_or_set_version(args):
    if not args.version:                                                    # take first version found
        releases = pypi.package_releases(args.name)
        if len(releases) == 0:
            print("unable to find a suitable release for {0}!".format(args.name))
            sys.exit(1)
        else:
            args.version = pypi.package_releases(args.name)[0]              # return first (current) release number


def newest_download_url(args):
    for url in pypi.package_urls(args.name, args.version):                  # Fetch all download URLs
        if url['packagetype'] == 'sdist':                                   # Found the source URL we care for
            return url
    # No PyPI tarball release, let's see if an upstream download URL is provided:
    data = pypi.release_data(args.name, args.version)                       # Fetch all meta data
    if 'download_url' in data:
        filename = os.path.basename(data['download_url'])
        return {'url': data['download_url'], 'filename': filename}
    return {}                                                               # We're all out of bubblegum


def file_template_list():
    return [filename for filename in os.listdir(TEMPLATE_DIR) if not filename.startswith('.')]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version='%(prog)s {0}'.format(__version__))
    parser.add_argument('--proxy', help='HTTP proxy to use')
    subparsers = parser.add_subparsers(title='commands')

    parser_list = subparsers.add_parser('list', help='list all packages on PyPI')
    parser_list.set_defaults(func=list)

    parser_search = subparsers.add_parser('search', help='search for packages on PyPI')
    parser_search.add_argument('name', help='package name (with optional version)')
    parser_search.set_defaults(func=search)

    parser_show = subparsers.add_parser('show', help='show metadata for package')
    parser_show.add_argument('name', help='package name')
    parser_show.add_argument('version', nargs='?', help='package version (optional)')
    parser_show.set_defaults(func=show)

    parser_fetch = subparsers.add_parser('fetch', help='download package source tarball from PyPI')
    parser_fetch.add_argument('name', help='package name')
    parser_fetch.add_argument('version', nargs='?', help='package version (optional)')
    parser_fetch.set_defaults(func=fetch)

    parser_generate = subparsers.add_parser('generate', help='generate RPM spec or DEB dsc file for a package')
    parser_generate.add_argument('name', help='package name')
    parser_generate.add_argument('version', nargs='?', help='package version (optional)')
    parser_generate.add_argument('-t', '--template', choices=file_template_list(), default='opensuse.spec', help='file template')
    parser_generate.add_argument('-f', '--filename', help='spec filename (optional)')
    # TODO (toabctl): remove this is a later release
    parser_generate.add_argument(
        '-r', '--run', action='store_true',
        help='DEPRECATED and noop. will be removed in future releases!')
    parser_generate.set_defaults(func=generate)

    parser_help = subparsers.add_parser('help', help='show this help')
    parser_help.set_defaults(func=lambda args: parser.print_help())

    args = parser.parse_args()

    # set HTTP proxy if one is provided
    if args.proxy:
        try:
            urllib.urlopen(args.proxy)
        except IOError:
            print('the proxy \'{0}\' is not responding'.format(args.proxy))
            sys.exit(1)
        transport = py2pack.proxy.ProxiedTransport()
        transport.set_proxy(args.proxy)
        pypi._ServerProxy__transport = transport  # Evil, but should do the trick

    args.func(args)


# fallback if run directly
if __name__ == '__main__':
    main()
