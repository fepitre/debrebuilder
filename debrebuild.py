#!/usr/bin/python3
#
# The Qubes OS Project, http://www.qubes-os.org
#
# Copyright (C) 2021 Frédéric Pierret (fepitre) <frederic.pierret@qubes-os.org>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

import os
import sys
import re
import requests
import tempfile
import json
import subprocess
import shutil
import argparse
import logging
import apt
import apt_pkg
from gemato.openpgp import IsolatedGPGEnvironment
from gemato.exceptions import OpenPGPVerificationFailure, \
    OpenPGPKeyImportError, OpenPGPUnknownSigFailure, OpenPGPUntrustedSigFailure

from debian.deb822 import Deb822
from dateutil.parser import parse as parsedate

logger = logging.getLogger(__name__)
console_handler = logging.StreamHandler(sys.stderr)
logger.addHandler(console_handler)


DEBIAN_VERSION = {
    "6": "squeeze",
    "7": "wheezy",
    "8": "jessie",
    "9": "stretch",
    "10": "buster",
    "11": "bullseye",
    "12": "bookworm"
}


class PackageException(Exception):
    pass


class BuildInfoException(Exception):
    pass


class RebuilderException(Exception):
    pass


def parsePkgAptHelper(pkg):
    name = None
    version = None
    architecture = None
    for item in pkg:
        parsed = item.split(':', 1)
        if len(parsed) == 2:
            field = parsed[0].strip()
            value = parsed[1].strip()
            if field == 'Package':
                name = value
            if field == 'Version':
                version = value
            if field == 'Architecture':
                architecture = value

    if name and version and architecture:
        return Package(name, version, architecture)


def parsePkgBuildDepend(pkg):
    # g++-mingw-w64-x86-64 (= 8.3.0-26+21.5+b1)
    parsed = re.match(r'^(.*) \(= (.*)\),*', pkg)
    if parsed:
        name = parsed.group(1).strip()
        version = parsed.group(2).strip()
        return Package(name, version)


class Package:
    def __init__(self, name, version, architecture=None):
        self.name = name
        self.version = version
        self.architecture = architecture
        self.first_seen = None
        self.hash = None

    def to_index_format(self):
        if self.architecture:
            result = "{} {} {}".format(
                self.name, self.version, self.architecture)
        else:
            result = "{} {}".format(self.name, self.version)
        return result

    def to_apt_install_format(self, build_arch):
        if self.architecture == "all" or self.architecture == build_arch:
            result = "{}={}".format(self.name, self.version)
        else:
            result = "{}:{}={}".format(
                self.name, self.architecture, self.version)
        return result

    def __repr__(self):
        return f'Package({self.name}, {self.version}, architecture={self.architecture})'


class BuildInfo:
    def __init__(self, buildinfo_file):
        self.orig_file = buildinfo_file

        self.source = None
        self.architecture = None
        self.binary = None
        self.version = None
        self.build_path = None
        self.build_arch = None
        self.build_date = None
        self.host_arch = None
        self.checksums = {}
        self.build_depends = []
        self.env = {}

        self.required_timestamps = []
        self.debian_suite = None

        if not os.path.exists(self.orig_file):
            raise BuildInfoException(
                "Cannot find buildinfo file: {}".format(self.orig_file))

        with open(self.orig_file) as fd:
            for paragraph in Deb822.iter_paragraphs(fd.read()):
                for item in paragraph.items():
                    if item[0] == 'Source':
                        self.source = item[1]
                    if item[0] == 'Architecture':
                        self.architecture = item[1].split()
                    if item[0] == 'Binary':
                        self.binary = item[1].split()
                    if item[0] == 'Version':
                        self.version = item[1]
                    if item[0] == 'Build-Path':
                        self.build_path = item[1]
                    if item[0] == 'Build-Architecture':
                        self.build_arch = item[1]
                    if item[0] == 'Build-Date':
                        self.build_date = item[1]
                    if item[0] == 'Host-Architecture':
                        self.host_arch = item[1]
                    if item[0].startswith('Checksums-'):
                        alg = item[0].replace('Checksums-', '').lower()
                        for line in item[1].lstrip('\n').split('\n'):
                            parsed_line = line.split()
                            if not self.checksums.get(parsed_line[2], {}):
                                self.checksums[parsed_line[2]] = {}
                            self.checksums[parsed_line[2]].update({
                                "size": parsed_line[1],
                                alg: parsed_line[0],
                            })
                    if item[0] == 'Installed-Build-Depends':
                        for pkg in item[1].lstrip('\n').split('\n'):
                            parsed_pkg = parsePkgBuildDepend(pkg)
                            if not parsed_pkg:
                                raise BuildInfoException(
                                    "Cannot parse package: %s" % pkg)
                            self.build_depends.append(parsed_pkg)
                    if item[0] == 'Environment':
                        for line in item[1].lstrip('\n').split('\n'):
                            parsed_line = re.match(r'^[^=](.*)="(.*)"', line)
                            if parsed_line:
                                self.env[parsed_line.group(1).strip()] = \
                                    parsed_line.group(2).strip()

        self.build_source = len(
            [arch for arch in self.architecture if arch == "source"]) == 1
        self.build_archall = len(
            [arch for arch in self.architecture if arch == "all"]) == 1
        self.architecture = [arch for arch in self.architecture if
                             arch != "source" and arch != "all"]

        if len(self.architecture) > 1:
            raise BuildInfoException(
                "More than one architecture in Architecture field")

        self.build_archany = len(self.architecture) == 1
        if not self.build_arch:
            raise BuildInfoException("Need Build-Architecture field")
        if not self.host_arch:
            self.host_arch = self.build_arch
        if not self.build_path:
            self.build_path = "/build/{}-{}".format(
                self.source, next(tempfile._get_candidate_names()))

    def get_debian_suite(self):
        if not self.debian_suite:
            for pkg in self.get_build_depends():
                if str(pkg.name) == "base-files":
                    try:
                        self.debian_suite = DEBIAN_VERSION[pkg.version]
                    except KeyError:
                        raise BuildInfoException(
                            "Cannot determine Debian version")
        return self.debian_suite

    def get_build_depends(self):
        return self.build_depends

    def get_build_date(self):
        try:
            return parsedate(self.build_date).strftime(
                "%Y%m%dT%H%M%SZ")
        except ValueError as e:
            raise RebuilderException("Cannot parse 'Build-Date': %s" % e)


class Rebuilder:
    def __init__(self, buildinfo_file, snapshot_url,
                 base_mirror="http://snapshot.debian.org/archive/debian",
                 extra_repository_files=None, extra_repository_keys=None,
                 gpg_sign_keyid=None,
                 gpg_verify=False,
                 gpg_verify_key=None,
                 proxy=None):
        self.buildinfo = None
        self.snapshot_url = snapshot_url
        self.base_mirror = base_mirror
        self.extra_repository_files = extra_repository_files
        self.extra_repository_keys = extra_repository_keys
        self.gpg_sign_keyid = gpg_sign_keyid
        self.proxy = proxy
        self.session = requests.Session()
        self.session.proxies = {
                "http:": self.proxy,
                "https": self.proxy
            }

        self.tempaptdir = None
        self.tempaptcache = None
        self.required_timestamp_sources = []

        self.tmpdir = os.environ.get('TMPDIR', '/tmp')

        if buildinfo_file.startswith('http://') or \
                buildinfo_file.startswith('https://'):
            try:
                resp = self.session.get(buildinfo_file)
                resp.raise_for_status()
                # We store remote buildinfo in a temporary file
                handle, buildinfo_file = tempfile.mkstemp(
                    prefix="buildinfo-", dir=self.tmpdir)
                with open(handle, 'w') as fd:
                    fd.write(resp.text)
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.HTTPError) as e:
                raise RebuilderException("Cannot get buildinfo: {}".format(e))
        else:
            buildinfo_file = realpath(buildinfo_file)

        if gpg_verify and gpg_verify_key:
            gpg_env = IsolatedGPGEnvironment()
            try:
                for key in gpg_verify_key:
                    with open(key, 'rb') as fd:
                        gpg_env.import_key(fd)
                with open(buildinfo_file, 'r') as fd:
                    data = gpg_env.verify_file(fd)
                    logger.info(
                        "GPG ({}): OK".format(data.primary_key_fingerprint))
            except OpenPGPKeyImportError:
                raise RebuilderException("Cannot import provided GPG keyring")
            except (OpenPGPVerificationFailure, OpenPGPUnknownSigFailure,
                    OpenPGPUntrustedSigFailure):
                raise RebuilderException(
                    "Failed to verify buildinfo GPG signature")
            finally:
                gpg_env.close()

        self.buildinfo = BuildInfo(buildinfo_file)
        if buildinfo_file.startswith(
                os.path.join(self.tmpdir, 'buildinfo-')):
            os.remove(buildinfo_file)

    def get_env(self):
        env = []
        for key, val in self.buildinfo.env.items():
            env.append("{}=\"{}\"".format(key, val))
        return env

    def get_response(self, url):
        resp = self.session.get(url)
        return resp

    def get_sources_list(self):
        sources_list = []
        url = "{}/{}".format(self.base_mirror, self.buildinfo.get_build_date())
        base_dist = self.buildinfo.get_debian_suite()
        release_url = "{}/dists/{}/Release".format(url, base_dist)
        resp = self.get_response(release_url)
        if resp.ok:
            sources_list.append("deb {}/ {} main".format(url, base_dist))
            sources_list.append("deb-src {}/ unstable main".format(url))
        resp.close()

        # WIP
        sources_list.append(
            "deb-src http://deb.debian.org/debian {} main".format(base_dist))

        if self.extra_repository_files:
            for repo_file in self.extra_repository_files:
                try:
                    with open(repo_file) as fd:
                        for line in fd:
                            if not line.startswith('#') and \
                                    not line.startswith('\n'):
                                sources_list.append(line.rstrip('\n'))
                except FileNotFoundError:
                    raise RebuilderException(
                        "Cannot find repository file: {}".format(repo_file))

        return sources_list

    def get_build_depends_timestamps(self):
        """
            Returns a list of tuple(timestamp, pkgs)
            where pkgs is a list of packages living there
        """
        required_timestamps = {}
        for pkg in self.buildinfo.get_build_depends():
            if not pkg.first_seen:
                self.get_bin_date(pkg)
            required_timestamps.setdefault(
                parsedate(pkg.first_seen).strftime("%Y%m%dT%H%M%SZ"), []).append(pkg)
        # sort by the number of packages found there, convert to list of tuples
        required_timestamps = sorted(required_timestamps.items(),
                key=lambda x: len(x[1]), reverse=True)
        return required_timestamps

    def get_sources_list_from_timestamp(self):
        """
            Returns a list of tuple(source_list, pkgs)
            where pkgs is a list of packages living there
        """
        sources_list = []
        # Check snapshot mirror validity
        for timestamp, pkgs in self.get_build_depends_timestamps():
            sources_list.append(
                ("deb {}/{} unstable main".format(self.base_mirror, timestamp),
                 pkgs))
        return sources_list

    def get_src_date(self):
        srcpkgname = self.buildinfo.source
        srcpkgver = self.buildinfo.version
        logger.debug("Get source package info: {}={}".format(
            srcpkgname, srcpkgver))
        json_url = "/mr/package/{}/{}/srcfiles?fileinfo=1".format(
            srcpkgname, srcpkgver)
        json_url = self.snapshot_url + json_url
        resp = self.get_response(json_url)
        try:
            data = resp.json()
        except json.decoder.JSONDecodeError:
            raise RebuilderException(
                "Cannot parse response for source: {}".format(self.buildinfo.source))

        package_from_main = []
        for result in data.get('result', []):
            for val in data.get('fileinfo', {}).get(result['hash']):
                if val['archive_name'] == 'debian' and \
                        val['name'].endswith('.dsc'):
                    package_from_main.append(val)
        if len(package_from_main) > 1:
            raise RebuilderException(
                "More than one package with the same hash in Debian official")
        if not package_from_main:
            raise RebuilderException(
                "No package with the right hash in Debian official")

        return package_from_main[0]['first_seen']

    def get_bin_date(self, package):
        pkgname = package.name
        pkgver = package.version
        pkgarch = package.architecture
        logger.debug("Get binary package info: {}={}".format(
            pkgname, pkgver))
        json_url = "/mr/binary/{}/{}/binfiles?fileinfo=1".format(
            pkgname, pkgver)
        json_url = self.snapshot_url + json_url
        resp = self.get_response(json_url)
        try:
            data = resp.json()
        except json.decoder.JSONDecodeError:
            raise RebuilderException(
                "Cannot parse response for package: {}".format(package.name))

        pkghash = None
        if len(data.get('result', [])) == 1:
            pkghash = data['result'][0]['hash']
            package.architecture = data['result'][0]['architecture']
            if pkgarch and pkgarch != package.architecture:
                raise RebuilderException(
                    "Package {} was explicitly requested {} but only {} was "
                    "found".format(pkgname, pkgarch, package.architecture))
            if not pkgarch and self.buildinfo.build_arch != package.architecture and \
                    "all" != package.architecture:
                raise RebuilderException(
                    "Package {} was implicitly requested {} but only {} was "
                    "found".format(
                        pkgname, self.buildinfo.build_arch, package.architecture))
            pkgarch = package.architecture
        else:
            if not pkgarch:
                pkgarch = self.buildinfo.build_arch
            for result in data.get('result', []):
                if result['architecture'] == pkgarch:
                    pkghash = result['hash']
                    break
            if not pkghash:
                raise RebuilderException(
                    "Cannot find package in architecture {}".format(pkgarch))
            package.architecture = pkgarch

        package_from_main = [pkg for pkg in data['fileinfo'].get(pkghash, []) if
                             pkg['archive_name'] == 'debian']
        if len(package_from_main) > 1:
            raise RebuilderException(
                "More than one package with the same hash in Debian official")
        if not package_from_main:
            raise RebuilderException(
                "No package with the right hash in Debian official")
        package.first_seen = package_from_main[0]['first_seen']
        package.hash = pkghash
        return package.first_seen

    def find_build_dependencies(self):
        notfound_packages = self.buildinfo.build_depends[:]
        temp_sources_list = self.tempaptdir + '/etc/apt/sources.list'
        with open(temp_sources_list, "a") as fd:
            for timestamp_source, pkgs in self.get_sources_list_from_timestamp():
                if not notfound_packages:
                    break
                if not any(pkg in notfound_packages for pkg in pkgs):
                    logger.info("Skipping snapshot: {}".format(timestamp_source))
                    continue
                logger.info("Remaining packages to be found: {}".format(
                    len(notfound_packages)))
                self.required_timestamp_sources.append(timestamp_source)
                logger.debug("Timestamp source ({} packages): {}".format(len(pkgs), timestamp_source))
                fd.write("\n{}".format(timestamp_source))
                fd.flush()

                # provides sources.list explicitly, otherwise `update()`
                # doesn't reload it until the next `open()`
                self.tempaptcache.update(sources_list=temp_sources_list)
                self.tempaptcache.open()

                for notfound_pkg in notfound_packages[:]:
                    pkg = self.tempaptcache.get("{}:{}".format(
                        notfound_pkg.name, notfound_pkg.architecture))
                    if pkg and pkg.versions.get(notfound_pkg.version):
                        notfound_packages.remove(notfound_pkg)
                    # else:
                    #     logger.debug("{} {} {}".format(
                    #         notfound_pkg.name, notfound_pkg.version,
                    #         notfound_pkg.architecture))

                self.tempaptcache.close()

        if notfound_packages:
            for notfound_pkg in notfound_packages:
                logger.debug(notfound_pkg.name)
            raise RebuilderException("Cannot locate the following packages via "
                                     "snapshots or the current repo/mirror")

    def prepare_aptcache(self):
        self.tempaptdir = tempfile.mkdtemp(
            prefix="debrebuild-", dir=self.tmpdir)

        # Create apt.conf
        temp_apt_conf = "{}/etc/apt/apt.conf".format(self.tempaptdir)
        # Create sources.list
        temp_sources_list = "{}/etc/apt/sources.list".format(self.tempaptdir)

        apt_dirs = [
            '/etc/apt', '/etc/apt/trusted.gpg.d'
        ]
        for directory in apt_dirs:
            os.makedirs("{}/{}".format(self.tempaptdir, directory))

        with open(temp_apt_conf, "w") as fd:
            apt_conf = """
Apt {{
   Architecture "{build_arch}";
   Architectures "{build_arch}";
}};

Acquire::Check-Valid-Until "false";
Acquire::Languages "none";
Acquire::http::Dl-Limit "1000";
Acquire::https::Dl-Limit "1000";
Acquire::Retries "5";
Binary::apt-get::Acquire::AllowInsecureRepositories "false";
""".format(build_arch=self.buildinfo.build_arch, tempdir=self.tempaptdir)
            if self.proxy:
                apt_conf += '\nAcquire::http::proxy "{}";\n'.format(self.proxy)
            fd.write(apt_conf)

        with open(temp_sources_list, "w") as fd:
            fd.write("\n".join(self.get_sources_list()))

        keyrings = [
            "/usr/share/keyrings/debian-archive-keyring.gpg",
            "/usr/share/keyrings/debian-archive-removed-keys.gpg",
        ]
        if self.extra_repository_keys:
            keyrings += self.extra_repository_keys
        for keyring_src in keyrings:
            keyring_dst = "{}/etc/apt/trusted.gpg.d/{}".format(
                self.tempaptdir, os.path.basename(keyring_src))
            os.symlink(keyring_src, keyring_dst)

        # Init temporary APT cache
        try:
            logger.debug("Initialize APT cache")
            self.tempaptcache = apt.Cache(rootdir=self.tempaptdir, memonly=True)
            self.tempaptcache.close()
        except (PermissionError, apt_pkg.Error):
            raise RebuilderException("Failed to initialize APT cache")

    def get_apt_build_depends(self):
        apt_build_depends = []
        for pkg in self.buildinfo.get_build_depends():
            apt_build_depends.append(
                pkg.to_apt_install_format(self.buildinfo.build_arch))
        return apt_build_depends

    def mmdebstrap(self, output, build_arch):
        if build_arch in ("source", "all", "any"):
            build = build_arch
        else:
            build = "binary"
        cmd = [
            'env', '-i',
            'PATH=/usr/sbin:/usr/bin:/sbin:/bin',
            'TMPDIR={}'.format(self.tmpdir),
            'mmdebstrap',
            '--arch={}'.format(self.buildinfo.build_arch),
            '--include={}'.format(' '.join(self.get_apt_build_depends())),
            '--variant=apt',
            '--aptopt=Acquire::Check-Valid-Until "false"',
            '--aptopt=Acquire::http::Dl-Limit "1000";',
            '--aptopt=Acquire::https::Dl-Limit "1000";',
            '--aptopt=Acquire::Retries "5";',
            '--aptopt=APT::Get::allow-downgrades "true";'
        ]
        if self.proxy:
            cmd += [
                '--aptopt=Acquire::http::proxy "{}";'.format(self.proxy)
            ]
        if self.extra_repository_keys:
            cmd += [
                '--essential-hook=copy-in {} /etc/apt/trusted.gpg.d/'.format(
                    ' '.join(self.extra_repository_keys))]

        # if self.extra_repository_files:
        #     cmd += [
        #         '--essential-hook=chroot "$1" sh -c "apt-get --yes install apt-transport-https ca-certificates"'
        #     ]

        cmd += [
            '--essential-hook=chroot "$1" sh -c \"{}\"'.format(" && ".join(
                [
                    'rm /etc/apt/sources.list',
                    "echo '{}' >> /etc/apt/sources.list".format('\n'.join(self.get_sources_list() + self.required_timestamp_sources)),
                    'apt-get update'
                ]
            ))
        ]

        cmd += [
            '--customize-hook=chroot "$1" sh -c \"{}\"'.format(" && ".join(
                [
                    'apt-get source --only-source -d {}={}'.format(self.buildinfo.source, self.buildinfo.version),
                    'mkdir -p {}'.format(os.path.dirname(self.buildinfo.build_path)),
                    'dpkg-source --no-check -x /*.dsc {}'.format(self.buildinfo.build_path),
                    'cd {}'.format(self.buildinfo.build_path),
                    'env {} dpkg-buildpackage -uc -a {} --build={}'.format(' '.join(self.get_env()), self.buildinfo.host_arch, build)
                ]
            ))
        ]

        cmd += [
            '--customize-hook=sync-out {} {}'.format(os.path.dirname(self.buildinfo.build_path), output),
            self.buildinfo.get_debian_suite(),
            '/dev/null',
            'deb {}/{}/ {} main'.format(self.base_mirror, self.buildinfo.get_build_date(), self.buildinfo.get_debian_suite())
        ]
        logger.debug(' '.join(cmd))
        if subprocess.run(cmd).returncode != 0:
            raise RebuilderException("mmdebstrap failed")

    def sbuild(self, output):
        cmd = [
            'sudo', 'env', '--chdir={}'.format(output)
        ]
        cmd += self.get_env()
        cmd += ["sbuild", "-D"]
        for repo in self.get_sources_list() + self.required_timestamp_sources:
            cmd += ['--extra-repository="{}"'.format(repo)]
        #
        # for key in self.extra_repository_keys:
        #     cmd += ['--extra-repository-key='.format(key)]

        cmd += [
            '--chroot-setup-commands=echo Acquire::Check-Valid-Until "false"\n--aptopt=Acquire::http::Dl-Limit "1000";\n--aptopt=Acquire::https::Dl-Limit "1000";\n--aptopt=Acquire::Retries "5";\n--aptopt=APT::Get::allow-downgrades "true"; | tee /etc/apt/apt.conf.d/23-debrebuild.conf',
            '--chroot-setup-commands=apt-get --yes remove build-essential libc6-dev gcc g++ make dpkg-dev',
            '--chroot-setup-commands=apt-get --yes autoremove',
            '--add-depends={}'.format(' '.join(self.get_apt_build_depends())),
            '--build={}'.format(self.buildinfo.build_arch),
            '--host={}'.format(self.buildinfo.host_arch),
        ]
        if self.buildinfo.build_source:
            cmd += ['--source']
        else:
            cmd += ['--no-source']
        if self.buildinfo.build_archany:
            cmd += ['--arch-any']
        else:
            cmd += ['--no-arch-any']
        if self.buildinfo.build_archall:
            cmd += ['--arch-all']
        else:
            cmd += ['--no-arch-all']
        cmd += [
            '--dist={}'.format(self.buildinfo.get_debian_suite()),
            '--no-run-lintian',
            '--no-run-autopkgtest',
            '--no-apt-upgrade',
            '--no-apt-distupgrade',
            '--bd-uninstallable-explainer=',
            '--build-dep-resolver=aspcud',
            '--build-path={}'.format(self.buildinfo.build_path),
            '{}_{}'.format(self.buildinfo.source, self.buildinfo.version)
        ]
        logger.debug(' '.join(cmd))
        if subprocess.run(cmd).returncode != 0:
            raise RebuilderException("sbuild failed")

    def verify_checksums(self, new_buildinfo):
        files = [f for f in self.buildinfo.checksums.keys() if not f.endswith('.dsc')]
        new_files = new_buildinfo.checksums.keys()
        if len(files) != len(new_files):
            logger.debug("old buildinfo: {}".format(' '.join(files)))
            logger.debug("new buildinfo: {}".format(' '.join(new_files)))
            raise RebuilderException(
                "New buildinfo contains a different number of files.")

        for f in files:
            for prop in self.buildinfo.checksums[f].keys():
                if prop == "size":
                    f_size = self.buildinfo.checksums[f]["size"]
                    if f_size != new_buildinfo.checksums[f]["size"]:
                        raise RebuilderException(
                            "Size differs for {}".format(f))
                    # logger.debug("{} size: {}".format(f, f_size))
                if prop not in new_buildinfo.checksums[f].keys():
                    raise RebuilderException(
                        "{} is not used in both buildinfo files".format(prop))
                if self.buildinfo.checksums[f][prop] != \
                        new_buildinfo.checksums[f][prop]:
                    raise RebuilderException(
                        "Value of {} differs for {}".format(prop, f))
            logger.info("{}: OK".format(f))
        logger.info("Checksums: OK")

    def generate_intoto_metadata(self, output, new_buildinfo):
        new_files = new_buildinfo.checksums.keys()
        cmd = [
            "in-toto-run", "--step-name=rebuild", "--no-command",
            "--products"
        ] + list(new_files)
        if self.gpg_sign_keyid:
            cmd += ["--gpg", self.gpg_sign_keyid]
        else:
            cmd += ["--gpg"]
        if subprocess.run(cmd, cwd=output).returncode != 0:
            raise RebuilderException("in-toto metadata generation failed")
        logger.info("in-toto metadata generation: OK")

    @staticmethod
    def get_host_architecture():
        try:
            builder_architecture = subprocess.check_output(
                ["dpkg", "--print-architecture"]).decode('utf8').rstrip('\n')
        except FileNotFoundError:
            raise RebuilderException("Cannot determinate builder host architecture")
        return builder_architecture

    def run(self, builder, output):
        # Predict new buildinfo name created by builder
        # Based on dpkg/scripts/dpkg-genbuildinfo.pl
        if self.buildinfo.architecture:
            build_arch = self.get_host_architecture()
        elif self.buildinfo.build_archall:
            build_arch = "all"
        elif self.buildinfo.build_source:
            build_arch = "source"
        else:
            raise RebuilderException("Nothing to build")

        new_buildinfo_file = "{}/{}_{}_{}.buildinfo".format(
            output, self.buildinfo.source, self.buildinfo.version, build_arch)
        logger.debug("New buildinfo file: {}".format(new_buildinfo_file))
        if os.path.exists(new_buildinfo_file):
            raise RebuilderException(
                "Refusing to overwrite existing buildinfo file")

        # Stage 1: Parse provided buildinfo file and setup the rebuilder
        try:
            self.get_src_date()
            self.prepare_aptcache()
            self.find_build_dependencies()
        except (apt_pkg.Error, apt.cache.FetchFailedException,
                requests.exceptions.ConnectionError):
            raise RebuilderException("Failed to fetch packages")
        except KeyboardInterrupt:
            raise RebuilderException("Interruption")
        finally:
            if self.tempaptdir and self.tempaptdir.startswith(
                    os.path.join(self.tmpdir, 'debrebuild-')):
                if self.tempaptcache:
                    self.tempaptcache.close()
                shutil.rmtree(self.tempaptdir)

        # Stage 2: Run the actual rebuild of provided buildinfo file
        if builder == "none":
            return
        if builder == "mmdebstrap":
            self.mmdebstrap(output, build_arch)
        if builder == "sbuild":
            self.sbuild(output)

        # Stage 3: Everything post-build actions with rebuild artifacts
        new_buildinfo = BuildInfo(realpath(new_buildinfo_file))
        self.verify_checksums(new_buildinfo)
        self.generate_intoto_metadata(output, new_buildinfo)


def get_args():
    parser = argparse.ArgumentParser(
        description='Given a buildinfo file from a Debian package, '
                    'generate instructions for attempting to reproduce '
                    'the binary packages built from the associated source '
                    'and build information.'
    )
    parser.add_argument(
        "buildinfo",
        help="Input buildinfo file. Local or remote file."
    )
    parser.add_argument(
        "--output",
        help="Directory for the build artifacts (default: ./)",
        default=os.path.join(os.getcwd(), './')
    )
    parser.add_argument(
        "--builder",
        help="Which building software should be used. (default: none)",
        default="none"
    )
    parser.add_argument(
        "--query-url",
        help="API url for querying package and binary information "
             "(default: http://snapshot.debian.org)",
        default="http://snapshot.debian.org"
    )
    parser.add_argument(
        "--extra-repository-file",
        help="Add repository file content to the list of apt sources during "
             "the package build.",
        action="append"
    )
    parser.add_argument(
        "--extra-repository-key",
        help="Add key file (.asc) to the list of trusted keys during "
             "the package build.",
        action="append"
    )
    parser.add_argument(
        "--gpg-sign-keyid",
        help="GPG keyid to use for signing in-toto metadata."
    )
    parser.add_argument(
        "--gpg-verify",
        help="Verify buildinfo GPG signature.",
        action="store_true"
    )
    parser.add_argument(
        "--gpg-verify-key",
        help="GPG key to use for buildinfo GPG check.",
        action="append"
    )
    parser.add_argument(
        "--proxy",
        help="Proxy address to use."
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Display logger info messages."
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Display logger debug messages."
    )
    return parser.parse_args()


def realpath(path):
    return os.path.abspath(os.path.expanduser(path))


def main():
    args = get_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)
    elif args.verbose:
        logger.setLevel(logging.INFO)
    else:
        logger.setLevel(logging.ERROR)

    if args.builder not in ("none", "mmdebstrap", "sbuild"):
        logger.error("Unknown builder: {}".format(args.builder))
        return 1

    if args.gpg_verify_key:
        args.gpg_verify_key = \
            [realpath(key_file) for key_file in args.gpg_verify_key]

    if args.extra_repository_file:
        args.extra_repository_file = \
            [realpath(repo_file) for repo_file in args.extra_repository_file]

    if args.extra_repository_key:
        args.extra_repository_key = \
            [realpath(key_file) for key_file in args.extra_repository_key]

    if args.gpg_verify and not args.gpg_verify_key:
        logger.error(
            "Cannot verify buildinfo signature without GPG keyring provided")
        return 1

    try:
        rebuilder = Rebuilder(
            buildinfo_file=args.buildinfo,
            snapshot_url=args.query_url,
            extra_repository_files=args.extra_repository_file,
            extra_repository_keys=args.extra_repository_key,
            gpg_sign_keyid=args.gpg_sign_keyid,
            gpg_verify=args.gpg_verify,
            gpg_verify_key=args.gpg_verify_key,
            proxy=args.proxy
        )
        rebuilder.run(builder=args.builder, output=realpath(args.output))
    except RebuilderException as e:
        logger.error(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
