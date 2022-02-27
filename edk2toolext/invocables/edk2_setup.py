# @file edk2_setup
# updates submodules listed as Required Submodules in Config file.
##
# Copyright (c) Microsoft Corporation
#
# SPDX-License-Identifier: BSD-2-Clause-Patent
##

import os
import yaml
import configparser
import logging
from io import StringIO
from typing import List
from edk2toolext import edk2_logging
from edk2toolext.environment import version_aggregator
from edk2toolext.invocables.edk2_multipkg_aware_invocable import Edk2MultiPkgAwareInvocable
from edk2toolext.invocables.edk2_multipkg_aware_invocable import MultiPkgAwareSettingsInterface
from edk2toollib.utility_functions import RunCmd
from edk2toollib.utility_functions import version_compare


class RequiredSubmodule():

    def __init__(self, path: str, recursive: bool = True):
        '''
        Object to hold necessary information for resolving submodules

        path:   workspace relative path to submodule that must be
                synchronized and updated
        recursive: boolean if recursion should be used in this submodule
        '''
        self.path = path
        self.recursive = recursive


class SetupSettingsManager(MultiPkgAwareSettingsInterface):
    ''' Platform settings will be accessed through this implementation. '''

    def GetRequiredSubmodules(self) -> List[RequiredSubmodule]:
        ''' return iterable containing RequiredSubmodule objects.
        If no RequiredSubmodules return an empty list
        '''
        return []


class Edk2PlatformSetup(Edk2MultiPkgAwareInvocable):
    ''' Updates git submodules listed in RequiredSubmodules '''

    def AddCommandLineOptions(self, parserObj):
        ''' adds command line options to the argparser '''
        def abs_file_path(path_arg: str):
            abs_path = os.path.abspath(path_arg)
            if not os.path.isfile(abs_path):
                raise ValueError(f"path '{path_arg}' does not point to a file")
            return abs_path

        parserObj.add_argument('--force', '--FORCE', '--Force', dest="force", action='store_true', default=False)
        parserObj.add_argument('--omnicache', '--OMNICACHE', '--Omnicache', dest='omnicache_path',
                               default=os.environ.get('OMNICACHE_PATH'))
        parserObj.add_argument("--special", dest="special", type=abs_file_path,
                               default=None,
                               help="[optional] path to the special setup config file")

        super().AddCommandLineOptions(parserObj)

    def RetrieveCommandLineOptions(self, args):
        '''  Retrieve command line options from the argparser '''
        self.force_it = args.force
        self.omnicache_path = args.omnicache_path
        if (self.omnicache_path is not None) and (not os.path.exists(self.omnicache_path)):
            logging.warning(f"Omnicache path set to invalid path: {args.omnicache_Path}")
            self.omnicache_path = None
        self.special_file = args.special

        super().RetrieveCommandLineOptions(args)

    def GetVerifyCheckRequired(self):
        ''' Will not call self_describing_environment.VerifyEnvironment because it hasn't been set up yet '''
        return False

    def GetSettingsClass(self):
        '''  Providing SetupSettingsManager  '''
        return SetupSettingsManager

    def GetLoggingFileName(self, loggerType):
        return "SETUPLOG"

    def Go(self):
        required_submodules = self.PlatformSettings.GetRequiredSubmodules()
        workspace_path = self.GetWorkspaceRoot()
        # Make sure git is installed
        return_buffer = StringIO()
        RunCmd("git", "--version", outstream=return_buffer, raise_exception_on_nonzero=True)
        git_version = return_buffer.getvalue().strip()
        return_buffer.close()
        version_aggregator.GetVersionAggregator().ReportVersion("Git",
                                                                git_version,
                                                                version_aggregator.VersionTypes.TOOL)
        min_git = "2.11.0"
        # This code is highly specific to the return value of "git version"...
        cur_git = ".".join(git_version.split(' ')[2].split(".")[:3])
        if version_compare(min_git, cur_git) > 0:
            raise RuntimeError("Please upgrade Git! Current version is %s. Minimum is %s." % (cur_git, min_git))

        # Pre-setup cleaning if "--force" is specified.
        if self.force_it:
            try:
                # Clean and reset the main repo.
                edk2_logging.log_progress("## Cleaning the root repo...")
                RunCmd("git", "reset --hard", workingdir=workspace_path,
                       logging_level=logging.DEBUG, raise_exception_on_nonzero=True)
                # Because logging is running right now, we have to skip the files that are open.
                ignore_files = "-e Build/%s.txt -e Build/%s.md" % (self.GetLoggingFileName('txt'),
                                                                   self.GetLoggingFileName('md'))
                RunCmd("git", "clean -xffd %s" % ignore_files, workingdir=workspace_path,
                       logging_level=logging.DEBUG, raise_exception_on_nonzero=True)
                edk2_logging.log_progress("Done.\n")

                # Clean any submodule repos.
                if required_submodules:
                    for required_submodule in required_submodules:
                        edk2_logging.log_progress("## Cleaning Git repository: %s..." % required_submodule.path)
                        required_submodule_path = os.path.normpath(
                            os.path.join(workspace_path, required_submodule.path))
                        RunCmd("git", "reset --hard", workingdir=required_submodule_path,
                               logging_level=logging.DEBUG, raise_exception_on_nonzero=True)
                        RunCmd("git", "clean -xffd", workingdir=required_submodule_path,
                               logging_level=logging.DEBUG, raise_exception_on_nonzero=True)

                        edk2_logging.log_progress("Done.\n")

            except RuntimeError as e:
                logging.error("FAILED!\n")
                logging.error("Error while trying to clean the environment!")
                logging.error(str(e))
                return

        # Grab the remaining Git repos.
        if required_submodules and len(required_submodules) > 0:
            # If a "special" file is provided, pivot to the special init path.
            if self.special_file is not None:
                return self.SpecialSetup(self.special_file, required_submodules)

            # Git Repos: STEP 1 --------------------------------------
            # Make sure that the repos are all synced.
            try:
                submodule_string = " ".join([x.path for x in required_submodules])
                edk2_logging.log_progress(f"## Syncing Git repositories: {submodule_string}...")
                RunCmd("git", f'submodule sync -- {submodule_string}',
                       workingdir=workspace_path, logging_level=logging.DEBUG, raise_exception_on_nonzero=True)

                edk2_logging.log_progress("Done.\n")
            except RuntimeError as e:
                logging.error("FAILED!\n")
                logging.error("Error while trying to synchronize the environment!")
                logging.error(str(e))
                return

            # Git Repos: STEP 2 --------------------------------------
            # Iterate through all repos and see whether they should be fetched.
            for required_submodule in required_submodules:
                try:
                    edk2_logging.log_progress(f"## Checking Git repository: {required_submodule.path}...")

                    # Git Repos: STEP 2a ---------------------------------
                    # Need to determine whether to skip this repo.
                    required_submodule_path = os.path.normpath(os.path.join(workspace_path, required_submodule.path))
                    skip_repo = False
                    # If the repo exists (and we're not forcing things) make
                    # sure that it's not in a "dirty" state.
                    if os.path.exists(required_submodule_path) and not self.force_it:
                        return_buffer = StringIO()
                        RunCmd("git", 'diff ' + required_submodule.path, outstream=return_buffer,
                               workingdir=workspace_path, logging_level=logging.DEBUG, raise_exception_on_nonzero=True)
                        git_data = return_buffer.getvalue().strip()
                        return_buffer.close()
                        # If anything was returned, we should skip processing the repo.
                        # It is either on a different commit or it has local changes.
                        if git_data != "":
                            logging.info("-- NOTE: Repo currently exists and appears to have local changes!")
                            logging.info("-- Skipping fetch!")
                            skip_repo = True

                    # Git Repos: STEP 2b ---------------------------------
                    # If we're not skipping, grab it.
                    if not skip_repo or self.force_it:
                        logging.info("## Fetching repo.")
                        cmd_string = "submodule update --init"
                        if required_submodule.recursive:
                            cmd_string += " --recursive"
                        cmd_string += " --progress"
                        if self.omnicache_path is not None:
                            cmd_string += " --reference " + self.omnicache_path
                        cmd_string += " " + required_submodule.path
                        ret = RunCmd('git', cmd_string, workingdir=workspace_path,
                                     logging_level=logging.DEBUG, raise_exception_on_nonzero=False)
                        if ret != 0:
                            logging.error("Failed to fetch " + required_submodule.path)
                            raise RuntimeError("Unable to checkout repo due to error")

                    edk2_logging.log_progress("Done.\n")

                except RuntimeError as e:
                    logging.error("FAILED!\n")
                    logging.error("Failed to fetch required repository!\n")
                    logging.error(str(e))

        return 0


    def SpecialSetup(self, special_config_path: str, required_submodules: List[RequiredSubmodule]) -> int:
        edk2_logging.log_progress(f"## Performing a special setup using file: {special_config_path}")
        with open(special_config_path, 'r') as yml_file:
            special_config = yaml.load(yml_file, yaml.SafeLoader)

        def get_repo_submodules(repo_path: str) -> List[dict]:
            results = []
            git_sm_config = configparser.ConfigParser()
            git_sm_config.read(os.path.join(repo_path, ".gitmodules"))
            for section in git_sm_config.sections():
                results.append({
                    'name': str(section).replace('submodule "', '').replace('"', ''),
                    'path': git_sm_config[section]['path'],
                    'url': git_sm_config[section]['url']
                })
            return results

        def get_url_substitution_from_list(url: str, sub_list: List[dict]):
            # TODO: Maybe support wildcards or patterns.
            # TODO: Maybe turn this into a dict object.
            for sub in sub_list:
                if url.lower() == sub['url'].lower():
                    return sub['sub']
            else:
                return None

        def special_init_submodule(root_path: str, submodule_info: dict, special_config: dict, recursive: bool = True):
            repo_path = os.path.normpath(os.path.join(root_path, submodule_info['path']))
            edk2_logging.log_progress(f"## Special init of repo '{repo_path}'")

            # First, we need to check the path info against the sub list.
            url_sub = get_url_substitution_from_list(submodule_info['url'], special_config['url_substitutions'])
            if url_sub is not None:
                # If found, first sub the url in the root_path.
                params = ["submodule", "set-url", "--", submodule_info['name'], url_sub]
                if RunCmd("git", " ".join(params), workingdir=root_path) != 0:
                    logging.error(f"Failed to update url for {repo_path}")
                    logging.error(f"-- From: {submodule_info['url']}")
                    logging.error(f"-- To: {url_sub}")
                    return False

            # Then init the repo in the root path.
            params = ["submodule", "update", "--init", "--", submodule_info['path']]
            # TODO: Should we also use the Omnicache?
            # TODO: Should we also process the skip directories (for dirty)?
            if RunCmd("git", " ".join(params), workingdir=root_path) != 0:
                logging.error(f"Failed to initialize repo {repo_path}")
                return False

            # If recursive, list submodules, update root_path, and repeat.
            if recursive:
                submodules = get_repo_submodules(repo_path=repo_path)
                for sm in submodules:
                    if not special_init_submodule(root_path=repo_path, submodule_info=sm,
                                                  special_config=special_config, recursive=recursive):
                        # If failed, propagate the failure.
                        return False

            return True

        try:
            # Get the repo submodules for the root.
            root_path = self.GetWorkspaceRoot()
            root_submodules = get_repo_submodules(repo_path=root_path)
            for sm in root_submodules:
                # Filter on required.
                for req_sm in required_submodules:
                    if sm['path'] == req_sm.path:
                        # Special init in order, recursively if flagged.
                        if not special_init_submodule(root_path=root_path, submodule_info=sm,
                                                      special_config=special_config, recursive=req_sm.recursive):
                            raise RuntimeError(f"failed to init '{os.path.join(root_path, sm['path'])}'")
        except RuntimeError as e:
            logging.error("FAILED!\n")
            logging.error(str(e))
            return -1

        return 0


def main():
    Edk2PlatformSetup().Invoke()
