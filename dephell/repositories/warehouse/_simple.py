# built-in
import asyncio
import posixpath
import re
from datetime import datetime
from logging import getLogger
from typing import Dict, Iterable, List, Optional, Tuple, Iterator
from urllib.parse import urlparse, urljoin, parse_qs, quote

# external
import attr
import html
import html5lib
import requests
from dephell_specifier import RangeSpecifier
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

# app
from ...cache import JSONCache, TextCache
from ...config import config
from ...exceptions import PackageNotFoundError
from ...models.release import Release
from ._base import WarehouseBaseRepo


logger = getLogger('dephell.repositories.warehouse.simple')
REX_WORD = re.compile('[a-zA-Z]+')


@attr.s()
class WarehouseSimpleRepo(WarehouseBaseRepo):
    name = attr.ib(type=str)
    url = attr.ib(type=str)

    prereleases = attr.ib(type=bool, factory=lambda: config['prereleases'])  # allow prereleases
    propagate = True  # deps of deps will inherit repo

    def __attrs_post_init__(self):
        # make name canonical
        if self.name in ('pypi.org', 'pypi.python.org'):
            self.name = 'pypi'

        # replace link on pypi api by link on simple index
        parsed = urlparse(self.url)
        if parsed.hostname == 'pypi.python.org':
            hostname = 'pypi.org'
        else:
            hostname = parsed.hostname
        if hostname in ('pypi.org', 'test.pypi.org'):
            path = '/simple/'
        else:
            path = parsed.path
        self.url = parsed.scheme + '://' + hostname + path

    @property
    def pretty_url(self) -> str:
        return self.url

    def get_releases(self, dep) -> tuple:
        # retrieve data
        cache = JSONCache(
            urlparse(self.url).hostname, 'links', dep.base_name,
            ttl=config['cache']['ttl'],
        )
        links = cache.load()
        if links is None:
            links = list(self._get_links(name=dep.base_name))
            cache.dump(links)

        releases_info = dict()
        for link in links:
            name, version = self._parse_name(link['name'])
            if canonicalize_name(name) != dep.name:
                continue
            if not version:
                continue

            if version not in releases_info:
                releases_info[version] = dict(hashes=[], pythons=[])
            if link['digest']:
                releases_info[version]['hashes'].append(link['digest'])
            if link['python']:
                releases_info[version]['pythons'].append(link['python'])

        # init releases
        releases = []
        prereleases = []
        for version, info in releases_info.items():
            # ignore version if no files for release
            release = Release(
                raw_name=dep.raw_name,
                version=version,
                time=datetime(1970, 1, 1, 0, 0),
                python=RangeSpecifier(' || '.join(info['pythons'])),
                hashes=tuple(info['hashes']),
                extra=dep.extra,
            )

            # filter prereleases if needed
            if release.version.is_prerelease:
                prereleases.append(release)
                if not self.prereleases and not dep.prereleases:
                    continue

            releases.append(release)

        # special case for black: if there is no releases, but found some
        # prereleases, implicitly allow prereleases for this package
        if not release and prereleases:
            releases = prereleases

        releases.sort(reverse=True)
        return tuple(releases)

    async def get_dependencies(self, name: str, version: str,
                               extra: Optional[str] = None) -> Tuple[Requirement, ...]:
        cache = TextCache(urlparse(self.url).hostname, 'deps', name, str(version))
        deps = cache.load()
        if deps is None:
            task = self._get_deps_from_links(name=name, version=version)
            deps = await asyncio.gather(asyncio.ensure_future(task))
            deps = deps[0]
            cache.dump(deps)
        elif deps == ['']:
            return ()
        return self._convert_deps(deps=deps, name=name, version=version, extra=extra)

    def search(self, query: Iterable[str]) -> List[Dict[str, str]]:
        raise NotImplementedError

    def _get_links(self, name: str) -> Iterator[Dict[str, str]]:
        dep_url = posixpath.join(self.url, quote(name)) + '/'
        response = requests.get(dep_url)
        if response.status_code == 404:
            raise PackageNotFoundError(package=name, url=dep_url)
        response.raise_for_status()
        document = html5lib.parse(response.text, namespaceHTMLElements=False)

        for tag in document.findall('.//a'):
            link = tag.get('href')
            if not link:
                continue

            python = tag.get('data-requires-python')
            parsed = urlparse(link)
            fragment = parse_qs(parsed.fragment)
            yield dict(
                url=urljoin(dep_url, link),
                name=parsed.path.strip('/').split('/')[-1],
                python=html.unescape(python) if python else '*',
                digest=fragment['sha256'][0] if 'sha256' in fragment else None,
            )

    @staticmethod
    def _parse_name(fname: str) -> Tuple[str, str]:
        fname = fname.strip()
        if fname.endswith('.whl'):
            fname = fname.rsplit('-', maxsplit=3)[0]
            name, _, version = fname.partition('-')
            return name, version

        fname = fname.rsplit('.', maxsplit=1)[0]
        if fname.endswith('.tar'):
            fname = fname.rsplit('.', maxsplit=1)[0]
        parts = fname.split('-')
        name = []
        for part in parts:
            if REX_WORD.match(part):
                name.append(part)
            else:
                break
        version = parts[len(name):]
        return '-'.join(name), '-'.join(version)

    async def _get_deps_from_links(self, name, version):
        from ...converters import SDistConverter, WheelConverter

        # retrieve data
        cache = JSONCache(
            urlparse(self.url).hostname, 'links', name,
            ttl=config['cache']['ttl'],
        )
        links = cache.load()
        if links is None:
            links = list(self._get_links(name=name))
            cache.dump(links)

        good_links = []
        for link in links:
            link_name, link_version = self._parse_name(link['name'])
            if canonicalize_name(link_name) != name:
                continue
            if link_version != version:
                continue
            good_links.append(link)

        sdist = SDistConverter()
        wheel = WheelConverter()
        rules = (
            (wheel, 'py3-none-any.whl'),
            (wheel, '-none-any.whl'),
            (wheel, '.whl'),
            (sdist, '.tar.gz'),
            (sdist, '.zip'),
        )

        for converer, ext in rules:
            for link in good_links:
                if not link['name'].endswith(ext):
                    continue
                try:
                    return await self._download_and_parse(
                        url=link['url'],
                        converter=converer,
                    )
                except FileNotFoundError as e:
                    logger.warning(e.args[0])
        return ()
