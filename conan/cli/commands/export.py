import json
import os
import tempfile

from conan.api.model import MultiPackagesList, PackagesList
from conan.api.output import ConanOutput, cli_out_write
from conan.cli.command import conan_command, OnceArgument
from conan.cli.args import add_reference_args
from conan.internal.util.runners import check_output_runner


def common_args_export(parser):
    parser.add_argument("path", help="Path to a folder containing a recipe (conanfile.py), "
                                     "or a git repository with the 'git+<url>[@<ref>][#subdirectory=<path>]' syntax. "
                                     "Defaults to current directory.",
                        default=".", nargs="?")
    add_reference_args(parser)


def _clone_git_export(path):
    """
    Parse a pip-style VCS URL ("git+<scheme>://<url>[@<ref>][#subdirectory=<path>]") and perform
    a lightweight (shallow, single-commit) clone into a temporary folder, returning the local path
    to be used as the export path.
    """
    url = path[len("git+"):]

    # Optional "#subdirectory=<path>" fragment (pip also supports other fragment params)
    subdirectory = None
    if "#" in url:
        url, fragment = url.split("#", 1)
        params = dict(p.split("=", 1) for p in fragment.split("&") if "=" in p)
        subdirectory = params.get("subdirectory")

    # Optional "@<ref>" branch/tag selector. The ref separator lives in the URL path, so we look
    # for "@" only after the first "/" of the path, to avoid matching userinfo (e.g. ssh://git@host)
    ref = None
    scheme_sep = url.find("://")
    path_start = url.find("/", scheme_sep + 3) if scheme_sep != -1 else url.find("/")
    at_index = url.find("@", path_start) if path_start != -1 else url.rfind("@")
    if at_index != -1:
        url, ref = url[:at_index], url[at_index + 1:]

    tmp_folder = tempfile.mkdtemp()
    ConanOutput().info(f"Cloning git repo into '{tmp_folder}'")
    clone_args = ["clone", "--depth", "1"]
    if ref:
        clone_args += ["--branch", ref]
    # Quote url and target in case they contain spaces; check_output_runner runs with shell=True.
    # The url is not echoed to the output to avoid leaking embedded credentials/tokens.
    clone_args += [f'"{url}"', f'"{tmp_folder}"']
    check_output_runner("git {}".format(" ".join(clone_args)))

    return os.path.join(tmp_folder, subdirectory) if subdirectory else tmp_folder


def json_export(data):
    cli_out_write(json.dumps({"reference": data["reference"].repr_notime()}))


def pkglist_export(data):
    cli_out_write(json.dumps(data["pkglist"], indent=4))


@conan_command(group="Creator", formatters={"json": json_export, "pkglist": pkglist_export})
def export(conan_api, parser, *args):
    """
    Export a recipe to the Conan package cache.
    """
    common_args_export(parser)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-r", "--remote", action="append", default=None,
                       help='Look in the specified remote or remotes server')
    group.add_argument("-nr", "--no-remote", action="store_true",
                       help='Do not use remote, resolve exclusively in the cache')
    parser.add_argument("-l", "--lockfile", action=OnceArgument,
                        help="Path to a lockfile.")
    parser.add_argument("--lockfile-out", action=OnceArgument,
                        help="Filename of the updated lockfile")
    parser.add_argument("--lockfile-partial", action="store_true",
                        help="Do not raise an error if some dependency is not found in lockfile")
    parser.add_argument("--build-require", action='store_true', default=False,
                        help='Whether the provided reference is a build-require')
    args = parser.parse_args(*args)

    # Only enable scoped output if None. If it is False, it means that
    # we have explicitly disabled (e.g. tests), so we should not enable it
    if ConanOutput._scoped_recipe_output is None:
        ConanOutput._scoped_recipe_output = True
    cwd = os.getcwd()
    export_path = _clone_git_export(args.path) if args.path.startswith("git+") else args.path
    path = conan_api.local.get_conanfile_path(export_path, cwd, py=True)
    remotes = conan_api.remotes.list(args.remote) if not args.no_remote else []
    lockfile = conan_api.lockfile.get_lockfile(lockfile=args.lockfile,
                                               conanfile_path=path,
                                               cwd=cwd,
                                               partial=args.lockfile_partial)
    ref, conanfile = conan_api.export.export(path=path,
                                             name=args.name, version=args.version,
                                             user=args.user, channel=args.channel,
                                             lockfile=lockfile,
                                             remotes=remotes)
    lockfile = conan_api.lockfile.update_lockfile_export(lockfile, conanfile, ref,
                                                         args.build_require)
    conan_api.lockfile.save_lockfile(lockfile, args.lockfile_out, cwd)

    exported_list = PackagesList()
    exported_list.add_ref(ref)

    pkglist = MultiPackagesList()
    pkglist.add("Local Cache", exported_list)
    ConanOutput._scoped_recipe_output = None

    return {
        "pkglist": pkglist.serialize(),
        "reference": ref
    }
