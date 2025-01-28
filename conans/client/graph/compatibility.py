import os
from collections import OrderedDict

from conan.api.output import ConanOutput
from conan.internal.cache.home_paths import HomePaths
from conans.client.graph.compute_pid import run_validate_package_id
from conans.client.loader import load_python_file
from conan.internal.errors import conanfile_exception_formatter, scoped_traceback
from conan.errors import ConanException
from conans.client.migrations import CONAN_GENERATED_COMMENT

# TODO: Define other compatibility besides applications
from conans.util.files import load, save

_default_compat = """\
# This file was generated by Conan. Remove this comment if you edit this file or Conan
# will destroy your changes.

from conan.tools.build import supported_cppstd, supported_cstd
from conan.errors import ConanException


def cppstd_compat(conanfile):
    # It will try to find packages with all the cppstd versions
    extension_properties = getattr(conanfile, "extension_properties", {})
    compiler = conanfile.settings.get_safe("compiler")
    compiler_version = conanfile.settings.get_safe("compiler.version")
    cppstd = conanfile.settings.get_safe("compiler.cppstd")
    if not compiler or not compiler_version:
        return []
    factors = []  # List of list, each sublist is a potential combination
    if cppstd is not None and extension_properties.get("compatibility_cppstd") is not False:
        cppstd_possible_values = supported_cppstd(conanfile)
        if cppstd_possible_values is None:
            conanfile.output.warning(f'No cppstd compatibility defined for compiler "{compiler}"')
        else: # The current cppst must be included in case there is other factor
            factors.append([{"compiler.cppstd": v} for v in cppstd_possible_values])

    cstd = conanfile.settings.get_safe("compiler.cstd")
    if cstd is not None and extension_properties.get("compatibility_cstd") is not False:
        cstd_possible_values = supported_cstd(conanfile)
        if cstd_possible_values is None:
            conanfile.output.warning(f'No cstd compatibility defined for compiler "{compiler}"')
        else:
            factors.append([{"compiler.cstd": v} for v in cstd_possible_values if v != cstd])
    return factors


def compatibility(conanfile):
    # By default, different compiler.cppstd are compatible
    # factors is a list of lists
    factors = cppstd_compat(conanfile)

    # MSVC 194->193 fallback compatibility
    compiler = conanfile.settings.get_safe("compiler")
    compiler_version = conanfile.settings.get_safe("compiler.version")
    if compiler == "msvc":
        msvc_fallback = {"194": "193"}.get(compiler_version)
        if msvc_fallback:
            factors.append([{"compiler.version": msvc_fallback}])

    # Append more factors for your custom compatibility rules here

    # Combine factors to compute all possible configurations
    combinations = _factors_combinations(factors)
    # Final compatibility settings combinations to check
    return [{"settings": [(k, v) for k, v in comb.items()]} for comb in combinations]


def _factors_combinations(factors):
    combinations = []
    for factor in factors:
        if not combinations:
            combinations = factor
            continue
        new_combinations = []
        for comb in combinations:
            for f in factor:
                new_comb = comb.copy()
                new_comb.update(f)
                new_combinations.append(new_comb)
        combinations.extend(new_combinations)
    return combinations
"""


def migrate_compatibility_files(cache_folder):
    compatible_folder = HomePaths(cache_folder).compatibility_plugin_path
    compatibility_file = os.path.join(compatible_folder, "compatibility.py")
    cppstd_compat_file = os.path.join(compatible_folder, "cppstd_compat.py")

    def _should_migrate_file(file_path):
        if not os.path.exists(file_path):
            return True
        content = load(file_path)
        first_line = content.lstrip().split("\n", 1)[0]
        return CONAN_GENERATED_COMMENT in first_line

    if _should_migrate_file(compatibility_file) and _should_migrate_file(cppstd_compat_file):
        if os.path.exists(compatibility_file) and load(compatibility_file) != _default_compat:
            ConanOutput().success("Migration: Successfully updated compatibility.py}")
        save(compatibility_file, _default_compat)
        if os.path.exists(cppstd_compat_file):
            os.remove(cppstd_compat_file)


class BinaryCompatibility:

    def __init__(self, compatibility_plugin_folder):
        compatibility_file = os.path.join(compatibility_plugin_folder, "compatibility.py")
        if not os.path.exists(compatibility_file):
            raise ConanException("The 'compatibility.py' plugin file doesn't exist. If you want "
                                 "to disable it, edit its contents instead of removing it")
        mod, _ = load_python_file(compatibility_file)
        self._compatibility = mod.compatibility

    def compatibles(self, conanfile):
        compat_infos = []
        if hasattr(conanfile, "compatibility"):
            with conanfile_exception_formatter(conanfile, "compatibility"):
                recipe_compatibles = conanfile.compatibility()
                compat_infos.extend(self._compatible_infos(conanfile, recipe_compatibles))

        try:
            plugin_compatibles = self._compatibility(conanfile)
        except Exception as e:
            msg = f"Error while processing 'compatibility.py' plugin for '{conanfile}'"
            msg = scoped_traceback(msg, e, scope="plugins/compatibility")
            raise ConanException(msg)
        compat_infos.extend(self._compatible_infos(conanfile, plugin_compatibles))
        if not compat_infos:
            return {}

        result = OrderedDict()
        original_info = conanfile.info
        original_settings = conanfile.settings
        original_settings_target = conanfile.settings_target
        original_options = conanfile.options
        for c in compat_infos:
            # we replace the conanfile, so ``validate()`` and ``package_id()`` can
            # use the compatible ones
            conanfile.info = c
            conanfile.settings = c.settings
            conanfile.settings_target = c.settings_target
            conanfile.options = c.options
            run_validate_package_id(conanfile)
            pid = c.package_id()
            if pid not in result and not c.invalid:
                result[pid] = c
        # Restore the original state
        conanfile.info = original_info
        conanfile.settings = original_settings
        conanfile.settings_target = original_settings_target
        conanfile.options = original_options
        return result

    @staticmethod
    def _compatible_infos(conanfile, compatibles):
        result = []
        if compatibles:
            for elem in compatibles:
                compat_info = conanfile.original_info.clone()
                compat_info.compatibility_delta = elem
                settings = elem.get("settings")
                if settings:
                    compat_info.settings.update_values(settings, raise_undefined=False)
                options = elem.get("options")
                if options:
                    compat_info.options.update(options_values=OrderedDict(options))
                result.append(compat_info)
                settings_target = elem.get("settings_target")
                if settings_target and compat_info.settings_target:
                    compat_info.settings_target.update_values(settings_target, raise_undefined=False)
        return result
