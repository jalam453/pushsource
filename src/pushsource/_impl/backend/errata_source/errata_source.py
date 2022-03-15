import os
import logging
from concurrent import futures

import six
from six.moves.urllib import parse
from more_executors import Executors

from .errata_client import ErrataClient

from ... import compat_attr as attr
from ...source import Source
from ...model import (
    ErratumPushItem,
    ContainerImagePushItem,
    ModuleMdSourcePushItem,
    RpmPushItem,
    OperatorManifestPushItem,
    conv,
)
from ...helpers import (
    list_argument,
    try_bool,
    force_https,
    as_completed_with_timeout_reset,
)

LOG = logging.getLogger("pushsource")


class ErrataSource(Source):
    """Uses an advisory from Errata Tool as the source of push items."""

    def __init__(
        self,
        url,
        errata,
        koji_source=None,
        rpm_filter_arch=None,
        legacy_container_repos=False,
        threads=4,
        timeout=60 * 60 * 4,
    ):
        """Create a new source.

        Parameters:
            url (src)
                Base URL of Errata Tool, e.g. "http://errata.example.com",
                "https://errata.example.com:8123".

            errata (str, list[str])
                Advisory ID(s) to be used as push item source.
                If a single string is given, multiple IDs may be
                comma-separated.

            koji_source (str)
                URL of a koji source associated with this Errata Tool
                instance.

            rpm_filter_arch (str, list[str])
                If provided, only RPMs for these given arch(es) will be produced;
                e.g. "x86_64", "src" and "noarch".

            legacy_container_repos (bool)
                If ``True``, any container push items generated by this source will
                use a legacy format for repository IDs.

                This is intended to better support certain legacy code and will be
                removed when no longer needed. Only use this if you know that you
                need it.

            threads (int)
                Number of threads used for concurrent queries to Errata Tool
                and koji.

            timeout (int)
                Number of seconds after which an error is raised, if no progress is
                made during queries to Errata Tool.
        """
        self._url = force_https(url)
        self._errata = list_argument(errata)
        self._client = ErrataClient(threads=threads, url=self._errata_service_url)

        self._rpm_filter_arch = list_argument(rpm_filter_arch, retain_none=True)

        # This executor doesn't use retry because koji & ET executors already do that.
        self._executor = Executors.thread_pool(
            name="pushsource-errata", max_workers=threads
        ).with_cancel_on_shutdown()

        # We set aside a separate thread pool for koji so that there are separate
        # queues for ET and koji calls, yet we avoid creating a new thread pool for
        # each koji source.
        self._koji_executor = (
            Executors.thread_pool(name="pushsource-errata-koji", max_workers=threads)
            .with_retry()
            .with_cancel_on_shutdown()
        )
        self._koji_cache = {}
        self._koji_source_url = koji_source

        self._legacy_container_repos = try_bool(legacy_container_repos)
        self._timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._client.shutdown()
        self._executor.shutdown(True)
        self._koji_executor.shutdown(True)

    @property
    def _errata_service_url(self):
        # URL for the errata_service XML-RPC endpoint provided by ET.
        #
        # Note the odd handling of scheme here. The reason is that
        # ET oddly provides different APIs over http and https.
        #
        # This XML-RPC API is available anonymously over *https* only,
        # but for compatibility the caller is allowed to provide http
        # or https scheme and we'll apply the scheme we know is actually
        # needed.
        #
        # The XML-RPC endpoint is no longer available over http as of
        # 11 Oct 2021. This class's constructor will still accept either
        # http or https, but will force the given url to use https going
        # forward.
        parsed = parse.urlparse(self._url)
        base = "{0.scheme}://{0.netloc}{0.path}".format(parsed)

        # Note: os.path.join will join these components with a '\' on Windows
        # systems, which is not intended. The tradeoff is that os.path.join
        # handles cases where path components end with trailing path separators
        # and doesn't doesn't duplicate them i.e. prevents something like
        # https://errata.example.com/some/path//errata/errata_service
        return os.path.join(base, "errata/errata_service")

    def _koji_source(self, **kwargs):
        if not self._koji_source_url:
            raise ValueError("A Koji source is required but none is specified")
        return Source.get(
            self._koji_source_url,
            cache=self._koji_cache,
            executor=self._koji_executor,
            **kwargs
        )

    @property
    def _advisory_ids(self):
        # TODO: other cases (comma-separated; plain string)
        return self._errata

    def _push_items_from_raw(self, raw):
        erratum = ErratumPushItem._from_data(raw.advisory_cdn_metadata)

        items = self._push_items_from_rpms(erratum, raw.advisory_cdn_file_list)

        # The erratum should go to all the same destinations as the rpms,
        # before FTP paths are added.
        erratum_dest = set(erratum.dest or [])
        for item in items:
            for dest in item.dest:
                erratum_dest.add(dest)
        erratum = attr.evolve(erratum, dest=sorted(erratum_dest))

        # Adjust push item destinations according to FTP paths from ET, if any.
        items = self._add_ftp_paths(items, erratum, raw)

        items = items + self._push_items_from_container_manifests(
            erratum, raw.advisory_cdn_docker_file_list
        )

        return [erratum] + items

    def _push_items_from_container_manifests(self, erratum, docker_file_list):
        if not docker_file_list:
            return []

        # Example of container list for one item to one repo:
        #
        # {
        #     "dotnet-21-container-2.1-77.1621419388": {
        #         "docker": {
        #             "target": {
        #                 "external_repos": {
        #                     "rhel8/dotnet-21": {
        #                         "container_full_sig_key": "199e2f91fd431d51",
        #                         "container_sig_key": "fd431d51",
        #                         "tags": ["2.1", "2.1-77.1621419388", "latest"],
        #                     },
        #                 },
        #                 "repos": /* like external_repos but uses pulp repo IDs */,
        #             }
        #         }
        #     }
        # }
        #

        # We'll be getting container metadata from these builds.
        with self._koji_source(
            container_build=list(docker_file_list.keys())
        ) as koji_source:

            out = []

            for item in koji_source:
                if isinstance(item, ContainerImagePushItem):
                    item = self._enrich_container_push_item(
                        erratum, docker_file_list, item
                    )
                elif isinstance(item, OperatorManifestPushItem):
                    # Accept this item but nothing special to do
                    pass
                else:
                    # If build contained anything else, ignore it
                    LOG.debug(
                        "Erratum %s: ignored unexpected item from koji source: %s",
                        erratum.name,
                        item,
                    )
                    continue

                item = attr.evolve(item, origin=erratum.name)
                out.append(item)

        return out

    def _enrich_container_push_item(self, erratum, docker_file_list, item):
        # metadata from koji doesn't contain info about where the image should be
        # pushed and a few other things - enrich it now
        errata_meta = docker_file_list.get(item.build) or {}
        target = (errata_meta.get("docker") or {}).get("target") or {}

        repos = target.get("external_repos") or {}

        if self._legacy_container_repos:
            repos = target.get("repos") or {}

        sig_keys = set()
        dest = []
        for repo_id, repo_data in repos.items():
            for tag in repo_data.get("tags") or []:
                dest.append("%s:%s" % (repo_id, tag))

            sig_key = repo_data.get("container_full_sig_key")
            if sig_key:
                sig_keys.add(sig_key)

        dest = sorted(set(dest))

        # If ET is not requesting to push this to any repos or tags at all,
        # it's considered an error.
        if not dest:
            raise ValueError(
                "Erratum %s requests container build %s but provides no repositories"
                % (erratum.name, item.build)
            )

        if len(sig_keys) > 1:
            # The API structure in Errata Tool would theoretically allow for multiple
            # signing keys on a single image. However, it's unclear if there's any use-case
            # for it, and all existing code in Pub for dealing with container images does not
            # even come close to handling it correctly, so we're going to flag this as
            # unsupported for now.
            raise ValueError(
                "Unsupported: erratum %s requests multiple signing keys (%s) on build %s"
                % (erratum.name, ", ".join(sorted(sig_keys)), item.build)
            )

        dest_signing_key = None if not sig_keys else list(sig_keys)[0]

        # koji source provided basic info on container image, ET provides policy on
        # where/how it should be pushed, combine them both to get final push item
        return attr.evolve(item, dest=dest, dest_signing_key=dest_signing_key)

    def _push_items_from_rpms(self, erratum, rpm_list):
        out = []

        for build_nvr, build_info in six.iteritems(rpm_list):
            out.extend(self._rpm_push_items_from_build(erratum, build_nvr, build_info))
            out.extend(
                self._module_push_items_from_build(erratum, build_nvr, build_info)
            )

        return out

    def _module_push_items_from_build(self, erratum, build_nvr, build_info):
        modules = (build_info.get("modules") or {}).copy()

        module_filenames = list(modules.keys())

        # We always request the modulemd.src.txt because we might need it later
        # depending on the ftp_paths response.
        module_filenames.append("modulemd.src.txt")

        out = []

        # Get a koji source which will yield all modules from the build
        with self._koji_source(
            module_build=[build_nvr], module_filter_filename=module_filenames
        ) as koji_source:

            for push_item in koji_source:
                # ET uses filenames to identify the modules here, we must do the same.
                basename = os.path.basename(push_item.src)
                dest = modules.pop(basename, [])

                # Fill in more push item details based on the info provided by ET.
                push_item = attr.evolve(push_item, dest=dest, origin=erratum.name)

                out.append(push_item)

        # Were there any requested modules we couldn't find?
        missing_modules = ", ".join(sorted(modules.keys()))
        if missing_modules:
            msg = "koji build {nvr} does not contain {missing} (requested by advisory {erratum})".format(
                nvr=build_nvr, missing=missing_modules, erratum=erratum.name
            )
            raise ValueError(msg)

        return out

    def _filter_rpms_by_arch(self, erratum, rpm_filenames):
        if self._rpm_filter_arch is None:
            return rpm_filenames

        out = []
        ok_arches = [conv.archstr(arch) for arch in self._rpm_filter_arch]

        for filename in rpm_filenames:
            components = filename.split(".")
            if len(components) >= 3 and components[-1] == "rpm":
                arch = components[-2]
                if conv.archstr(arch) in ok_arches:
                    out.append(filename)
                    continue

            LOG.debug(
                "Erratum %s: RPM removed by arch filter: %s", erratum.name, filename
            )

        return out

    def _rpm_push_items_from_build(self, erratum, build_nvr, build_info):
        rpms = build_info.get("rpms") or {}
        signing_key = build_info.get("sig_key") or None
        sha256sums = (build_info.get("checksums") or {}).get("sha256") or {}
        md5sums = (build_info.get("checksums") or {}).get("md5") or {}

        rpm_filenames = self._filter_rpms_by_arch(erratum, list(rpms.keys()))

        # Get a koji source which will yield all desired push items from this build.
        koji_source = self._koji_source(rpm=rpm_filenames, signing_key=signing_key)

        out = []

        for push_item in koji_source:
            # Do not allow to proceed if RPM was absent
            if push_item.state == "NOTFOUND":
                raise ValueError(
                    "Advisory refers to %s but RPM was not found in koji"
                    % push_item.name
                )

            # Note, we can't sanity check here that the push item's build
            # equals ET's NVR, because it's not always the case.
            #
            # Example:
            #  RPM: pgaudit-debuginfo-1.4.0-4.module+el8.1.1+4794+c82b6e09.x86_64.rpm
            #  belongs to build: 1015162 (pgaudit-1.4.0-4.module+el8.1.1+4794+c82b6e09)
            #  but ET refers instead to module build: postgresql-12-8010120191120141335.e4e244f9.
            #
            # We also make use of this to fill in the module_build attribute on items when
            # available.
            #
            # (This is not ideal because we don't really "know" that a non-matching NVR here is
            # the module build NVR, we are relying on the ET implementation detail that this is
            # the only reason they should not match; though legacy code already depended on this
            # for years, so maybe it's fine. We also have the heuristic of scanning for 'module'
            # in the NVR to make exceptions less likely.)
            module_build = None
            if push_item.build != build_nvr and ".module" in push_item.build:
                module_build = build_nvr

            # Fill in more push item details based on the info provided by ET.
            push_item = attr.evolve(
                push_item,
                sha256sum=sha256sums.get(push_item.name),
                md5sum=md5sums.get(push_item.name),
                dest=rpms.get(push_item.name),
                origin=erratum.name,
                module_build=module_build,
            )

            out.append(push_item)

        return out

    def _add_ftp_paths(self, items, erratum, raw):
        ftp_paths = raw.ftp_paths

        # ftp_paths structure is like this:
        #
        # {
        #     "xorg-x11-server-1.20.4-16.el7_9": {
        #         "rpms": {
        #             "xorg-x11-server-1.20.4-16.el7_9.src.rpm": [
        #                 "/ftp/pub/redhat/linux/enterprise/7Client/en/os/SRPMS/",
        #                 "/ftp/pub/redhat/linux/enterprise/7ComputeNode/en/os/SRPMS/",
        #                 "/ftp/pub/redhat/linux/enterprise/7Server/en/os/SRPMS/",
        #                 "/ftp/pub/redhat/linux/enterprise/7Workstation/en/os/SRPMS/",
        #             ]
        #         },
        #         "modules": [
        #            "/ftp/pub/redhat/linux/enterprise/AppStream-8.0.0.Z/en/os/modules/"
        #         ],
        #         "sig_key": "fd431d51",
        #     }
        # }
        #
        # We use the (rpm, module) => ftp path mappings, which should be added onto
        # our existing push items if they match.
        #
        rpm_to_paths = {}
        build_to_module_paths = {}
        builds_need_modules = set()
        builds_have_modules = set()
        for build_nvr, build_map in ftp_paths.items():
            for (rpm_name, paths) in (build_map.get("rpms") or {}).items():
                rpm_to_paths[rpm_name] = paths

            modules = build_map.get("modules") or []
            build_to_module_paths[build_nvr] = modules
            if modules:
                builds_need_modules.add(build_nvr)

        out = []
        for item in items:
            if isinstance(item, RpmPushItem):
                # RPMs have dest updated with FTP paths.
                paths = rpm_to_paths.get(item.name) or []
                item = attr.evolve(item, dest=item.dest + paths)
                out.append(item)
            elif isinstance(item, ModuleMdSourcePushItem):
                # modulemd sources have dest updated with FTP paths and will
                # also be filtered out if there are no matches at all (because
                # modulemd sources aren't delivered in any other manner)
                paths = build_to_module_paths.get(item.build)
                if paths:
                    builds_have_modules.add(item.build)
                    item = attr.evolve(item, dest=item.dest + paths)
                if item.dest:
                    out.append(item)
                else:
                    # modulemd sources are filtered out altogether if ET did
                    # not provide any destinations.
                    LOG.debug(
                        "Erratum %s: modulemd source skipped due to no destinations: %s",
                        erratum.name,
                        item.src,
                    )
            else:
                # Other types of items are unaffected by ftp_paths.
                out.append(item)

        builds_missing_modules = sorted(builds_need_modules - builds_have_modules)
        builds_missing_et = []
        builds_missing_koji = []

        for nvr in builds_missing_modules:
            # If ET requests that modules should be pushed for any builds and we're
            # missing a modulemd.src.txt for those, there are two possible reasons for
            # that:
            #
            if not (raw.advisory_cdn_file_list.get(nvr) or {}).get("modules"):
                #
                # (1) The same modules were not present in get_advisory_cdn_file_list.
                #
                builds_missing_et.append(nvr)
            else:
                #
                # (2) modulemd.src.txt is genuinely missing from koji.
                #
                builds_missing_koji.append(nvr)

        if builds_missing_et:
            # Builds missing in ET are tolerated with just a warning.
            #
            # Although this probably *should* not happen, due to lack of any specification
            # for how these APIs are meant to work, it's a bit risky to treat it as an error.
            #
            # Note also that we *could* try to proceed here anyway, and now look up information
            # for this module from koji. The reason we don't do that is because the destination
            # for source modules (& RPMs) has been historically calculated by combining *both*
            # the FTP paths and repo IDs and passing them through alt-src config. If we only
            # have one of these sources of info and we proceed anyway, then we might push to
            # incorrect destinations while giving the false impression that everything is working
            # OK. Safer to not touch the item.
            #
            LOG.warning(
                "Erratum %s: ignoring module(s) from ftp_paths due to absence "
                "in cdn_file_list: %s",
                erratum.name,
                ", ".join(builds_missing_et),
            )

        if builds_missing_koji:
            # Builds missing in koji are fatal as there is no reason this should happen; the
            # koji build might be malformed, incomplete or there have been some
            # backwards-incompatible changes in the structure of module builds.
            msg = "Erratum %s: missing modulemd sources on koji build(s): %s" % (
                erratum.name,
                ", ".join(builds_missing_koji),
            )
            raise ValueError(msg)

        return out

    def __iter__(self):
        # Get raw ET responses for all errata.
        raw_fs = [self._client.get_raw_f(id) for id in self._advisory_ids]

        # Convert them to lists of push items
        push_items_fs = []
        for f in futures.as_completed(raw_fs, timeout=self._timeout):
            push_items_fs.append(
                self._executor.submit(self._push_items_from_raw, f.result())
            )

        completed_fs = as_completed_with_timeout_reset(
            push_items_fs, timeout=self._timeout
        )
        for f in completed_fs:
            for pushitem in f.result():
                yield pushitem


Source.register_backend("errata", ErrataSource)
