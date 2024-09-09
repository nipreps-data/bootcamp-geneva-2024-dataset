# emacs: -*- mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
#
# Copyright 2023 The Axon Lab <theaxonlab@gmail.com>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# We support and encourage derived works from this project, please read
# about our expectations at
#
#     https://www.nipreps.org/community/licensing/
#
# STATEMENT OF CHANGES: This file is derived from sources licensed under the Apache-2.0 terms,
# and this file has been changed.
# The original file this work derives from is found at:
# https://github.com/nipy/heudiconv/blob/55524168b02519bbf0a3a1c94cafb29a419728a0/heudiconv/heuristics/reproin.py
#
# ORIGINAL WORK'S ATTRIBUTION NOTICE:
#
#     Copyright [2014-2019] [Heudiconv developers]
#
#     Licensed under the Apache License, Version 2.0 (the "License");
#     you may not use this file except in compliance with the License.
#     You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#     Unless required by applicable law or agreed to in writing, software
#     distributed under the License is distributed on an "AS IS" BASIS,
#     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#     See the License for the specific language governing permissions and
#     limitations under the License.
"""Reproin heuristic."""

from __future__ import annotations

from warnings import warn
from collections import Counter
import logging
import re

import pydicom as dcm

from heudiconv.utils import SeqInfo
from heudiconv.heuristics.reproin import (
    _apply_substitutions,
    get_study_hash,
    get_study_description,
)

lgr = logging.getLogger("heudiconv")


DWI_RES = {
    "1.6mm-iso": "highres",
    "2mm-iso": "lowres",
}


IGNORE_PROTOCOLS = (
    "DEV",
    "LABEL",
    "REPORT",
    "ADC",
    "TRACEW",
    "FA",
    "ColFA",
    "B0",
    "TENSOR",
    "10meas",  # dismiss a trial of fmap acquisition
    "testJB",  # dismiss a test trial of the cmrr sequence
)

bids_regex = re.compile(r"_(?=(dir|acq|task|inv|run)-([A-Za-z0-9]+))")


# Terminology to harmonise and use to name variables etc
# experiment
#  subject
#   [session]
#    exam (AKA scanning session) - currently seqinfo, unless brought together from multiple
#     series  (AKA protocol?)
#      - series_spec - deduced from fields the spec (literal value)
#      - series_info - the dictionary with fields parsed from series_spec

# Which fields in seqinfo (in this order) to check for the ReproIn spec
series_spec_fields = ("protocol_name", "series_description")

# dictionary from accession-number to runs that need to be marked as bad
# NOTE: even if filename has number that is 0-padded, internally no padding
# is done
fix_accession2run: dict[str, list[str]] = {
    # e.g.:
    # 'A000035': ['^8-', '^9-'],
}

# A dictionary containing fixes/remapping for sequence names per study.
# Keys are md5sum of study_description from DICOMs, in the form of PI-Experimenter^protocolname
# You can use `heudiconv -f reproin --command ls --files  PATH
# to list the "study hash".
# Values are list of tuples in the form (regex_pattern, substitution).
# If the  key is an empty string`''''`, it would apply to any study.
protocols2fix: dict[str | re.Pattern[str], list[tuple[str, str]]] = {
    "": [
        ("anat-T1w_acq-mp2rage_run-01", "anat-mp2rage"),
        ("_UNI_", "_acq-mp2rage__"),
        ("_UNI-DEN", "_acq-denoised__"),
        ("_INV", "_inv-"),
        ("_T1_", "_acq-T1map__"),
        ("fmap_acq-siemens", "fmap-phasediff"),
        ("fmap-gre_acq-siemens", "fmap-phasediff"),
        ("_acq-midRes", ""),
        ("_acq-p6", ""),
        ("_run-01", ""),
    ]
    # e.g., QA:
    # '43b67d9139e8c7274578b7451ab21123':
    #     [
    #      ('BOLD_p2_s4_3\.5mm', 'func_task-rest_acq-p2-s4-3.5mm'),
    #      ('BOLD_', 'func_task-rest'),
    #      ('_p2_s4',        '_acq-p2-s4'),
    #      ('_p2', '_acq-p2'),
    #     ],
    # '':  # for any study example with regexes used
    #     [
    #         ('AAHead_Scout_.*', 'anat-scout'),
    #         ('^dti_.*', 'dwi'),
    #         ('^.*_distortion_corr.*_([ap]+)_([12])', r'fmap-epi_dir-\1_run-\2'),
    #         ('^(.+)_ap.*_r(0[0-9])', r'func_task-\1_run-\2'),
    #         ('^t1w_.*', 'anat-T1w'),
    #         # problematic case -- multiple identically named pepolar fieldmap runs
    #         # I guess we will just sacrifice ability to detect canceled runs here.
    #         # And we cannot just use _run+ since it would increment independently
    #         # for ap and then for pa.  We will rely on having ap preceding pa.
    #         # Added  _acq-mb8  so they match the one in funcs
    #         ('func_task-discorr_acq-ap', r'fmap-epi_dir-ap_acq-mb8_run+'),
    #         ('func_task-discorr_acq-pa', r'fmap-epi_dir-pa_acq-mb8_run='),
    # ]
}

# list containing StudyInstanceUID to skip -- hopefully doesn't happen too often
dicoms2skip: list[str] = [
    # e.g.
    # '1.3.12.2.1107.5.2.43.66112.30000016110117002435700000001',
]

DEFAULT_FIELDS = {
    # Let it just be in each json file extracted
    "Acknowledgements": "Provided by Emilie C. Sleight for the fMRIPrep bootcamp",
}

POPULATE_INTENDED_FOR_OPTS = {
    "matching_parameters": ["ImagingVolume", "Shims"],
    "criterion": "Closest",
}


def filter_dicom(dcmdata: dcm.dataset.Dataset) -> bool:
    """Return True if a DICOM dataset should be filtered out, else False"""
    return True if dcmdata.StudyInstanceUID in dicoms2skip else False


def filter_files(_fn: str) -> bool:
    """Return True if a file should be kept, else False.

    ATM reproin does not do any filtering. Override if you need to add some
    """
    return not _fn.endswith((".csv", ".dvs"))


def fix_canceled_runs(seqinfo: list[SeqInfo]) -> list[SeqInfo]:
    """Function that adds cancelme_ to known bad runs which were forgotten"""
    if not fix_accession2run:
        return seqinfo  # nothing to do
    for i, s in enumerate(seqinfo):
        accession_number = s.accession_number
        if accession_number and accession_number in fix_accession2run:
            lgr.info(
                "Considering some runs possibly marked to be "
                "canceled for accession %s",
                accession_number,
            )
            # This code is reminiscent of prior logic when operating on
            # a single accession, but left as is for now
            badruns = fix_accession2run[accession_number]
            badruns_pattern = "|".join(badruns)
            if re.match(badruns_pattern, s.series_id):
                lgr.info("Fixing bad run {0}".format(s.series_id))
                fixedkwargs = dict()
                for key in series_spec_fields:
                    fixedkwargs[key] = "cancelme_" + getattr(s, key)
                seqinfo[i] = s._replace(**fixedkwargs)
    return seqinfo


def fix_dbic_protocol(seqinfo: list[SeqInfo]) -> list[SeqInfo]:
    """Ad-hoc fixup for existing protocols.

    It will operate in 3 stages on `protocols2fix` records.
    1. consider a record which has md5sum of study_description
    2. apply all substitutions, where key is a regular expression which
       successfully searches (not necessarily matches, so anchor appropriately)
       study_description
    3. apply "catch all" substitutions in the key containing an empty string

    3. is somewhat redundant since `re.compile('.*')` could match any, but is
    kept for simplicity of its specification.
    """

    study_hash = get_study_hash(seqinfo)
    study_description = get_study_description(seqinfo)

    # We will consider first study specific (based on hash)
    if study_hash in protocols2fix:
        _apply_substitutions(
            seqinfo, protocols2fix[study_hash], "study (%s) specific" % study_hash
        )
    # Then go through all regexps returning regex "search" result
    # on study_description
    for sub, substitutions in protocols2fix.items():
        if isinstance(sub, re.Pattern) and sub.search(study_description):
            _apply_substitutions(
                seqinfo, substitutions, "%r regex matching" % sub.pattern
            )
    # and at the end - global
    if "" in protocols2fix:
        _apply_substitutions(seqinfo, protocols2fix[""], "global")

    return seqinfo


def fix_seqinfo(seqinfo: list[SeqInfo]) -> list[SeqInfo]:
    """Just a helper on top of both fixers"""
    # add cancelme to known bad runs
    seqinfo = fix_canceled_runs(seqinfo)
    seqinfo = fix_dbic_protocol(seqinfo)
    return seqinfo


def create_key(template, outtype=("nii.gz",), annotation_classes=None):
    if template is None or not template:
        raise ValueError("Template must be a valid format string")
    return template, outtype, annotation_classes


def infotodict(seqinfo):
    """Heuristic evaluator for determining which runs belong where

    allowed template fields - follow python string module:

    item: index within category
    subject: participant id
    seqitem: run number during scanning
    subindex: sub index within group
    """
    seqinfo = fix_seqinfo(seqinfo)
    lgr.info("Processing %d seqinfo entries", len(seqinfo))

    t1w = create_key(
        "sub-{subject}/anat/sub-{subject}_acq-{acquisition}{run_entity}_T1w"
    )
    t1map = create_key(
        "sub-{subject}/anat/sub-{subject}{run_entity}{part_entity}_T1map"
    )
    t2w = create_key(
        "sub-{subject}/anat/sub-{subject}_T2w"
    )
    t2_flair = create_key(
        "sub-{subject}/anat/sub-{subject}{run_entity}_FLAIR"
    )
    mp2rage = create_key(
        "sub-{subject}/anat/sub-{subject}_inv-{inversion}_MP2RAGE"
    )
    dwi = create_key(
        "sub-{subject}/dwi/sub-{subject}_acq-{acq}_dir-{dir}{run_entity}_dwi"
    )
    mag = create_key(
        "sub-{subject}/fmap/sub-{subject}{run_entity}_magnitude"
    )
    phdiff = create_key(
        "sub-{subject}/fmap/sub-{subject}{run_entity}_phasediff"
    )
    epi = create_key(
        "sub-{subject}/fmap/sub-{subject}"
        "_acq-{acquisition}_dir-{dir}{run_entity}{part_entity}_epi"
    )
    func = create_key(
        "sub-{subject}/func/sub-{subject}"
        "_task-{task}{acq_entity}{dir_entity}{run_entity}{part_entity}_bold"
    )
    sbref = create_key(
        "sub-{subject}/func/sub-{subject}_task-{task}{run_entity}_sbref"
    )

    info = {
        t1w: [],
        t2w: [],
        t2_flair: [],
        dwi: [],
        mag: [],
        phdiff: [],
        epi: [],
        func: [],
        sbref: [],
        mp2rage: [],
        t1map: [],
    }
    epi_mags = []
    bold_mags = []

    for s in seqinfo:
        """
        The namedtuple `s` contains the following fields:

        * total_files_till_now
        * example_dcm_file
        * series_id
        * dcm_dir_name
        * unspecified2
        * unspecified3
        * dim1
        * dim2
        * dim3
        * dim4
        * TR
        * TE
        * protocol_name
        * is_motion_corrected
        * is_derived
        * patient_id
        * study_description
        * referring_physician_name
        * series_description
        * image_type
        """

        # Ignore some protocols
        if s.dcm_dir_name.split("_")[-1] in IGNORE_PROTOCOLS:
            continue

        thisitem = {
            "item": s.series_id,
        }
        thiskey = None
        thisitem.update({k: v for k, v in bids_regex.findall(s.series_description)})
        thisitem["run_entity"] = f"{thisitem.pop('run', '')}"

        if s.protocol_name.lower().startswith("anat-t1w"):
            thiskey = t1w
            acquisition_present = thisitem.pop("acq", None)
            thisitem["acquisition"] = (
                ("original" if s.dcm_dir_name.endswith("_ND") else "undistorted")
                if not acquisition_present
                else acquisition_present
            )
        elif s.protocol_name.lower().startswith("anat-t2w"):
            thiskey = t2w
            acquisition_present = thisitem.pop("acq", None)
            thisitem["acquisition"] = (
                ("original" if s.dcm_dir_name.endswith("_ND") else "undistorted")
                if not acquisition_present
                else "unspecified"
            )
        elif s.protocol_name.lower().startswith("anat-mp2rage"):
            lgr.info(f"Processing {s.series_description}: {thisitem}")
            acquisition = thisitem.pop("acq", None) 
            if (inversion := thisitem.pop("inv", None)) is not None:
                thiskey = mp2rage
                thisitem["inversion"] = inversion
            elif acquisition in ("mp2rage", "denoised"): 
                thiskey = t1w
                thisitem["acquisition"] = acquisition
            elif acquisition == "T1map":
                thiskey = t1map
                thisitem["part_entity"] = ""
            else:
                lgr.warning(f"Could not interpret {s.protocol_name}, {s.series_description}")
        elif s.protocol_name.lower().startswith("anat-flair"):
            thiskey = t2_flair
        elif s.protocol_name.startswith("dwi-dwi"):
            thiskey = dwi
        elif s.protocol_name.startswith("fmap-phasediff"):
            thiskey = phdiff if "P" in s.image_type else mag
        elif s.protocol_name.startswith("fmap-epi"):
            thiskey = epi
            thisitem["part_entity"] = ""
            thisitem["acquisition"] = (
                "b0" if s.sequence_name.endswith("ep_b0") else "bold"
            )

            # Check whether phase was written out:
            # 1. A magnitude needs to exist immediately before in the dicom info
            # 2. Magnitude and phase must have the same number of volumes
            series_id_idx, series_id_name = s.series_id.split("-", 1)
            prev_series_id = f"{int(series_id_idx) - 1}-{series_id_name}-{s.series_files}"
            if prev_series_id in epi_mags:
                thisitem["part_entity"] = "_part-phase"
                info[thiskey][epi_mags.index(prev_series_id)]["part_entity"] = "_part-mag"

            epi_mags.append(f"{s.series_id}-{s.series_files}")

        elif s.protocol_name.startswith("func-bold"):
            # Likely an error
            if s.series_files < 100:
                warn(
                    f"Dropping exceedingly short BOLD file with {s.series_files} time points."
                )
                continue

            thiskey = func

            thisitem["part_entity"] = ""
            # Some functional runs may come with acq
            func_acq = thisitem.pop("acq", None)
            thisitem["acq_entity"] = "" if not func_acq else f"_acq-{func_acq}"

            # Some functional runs may come with dir
            func_dir = thisitem.pop("dir", None)
            thisitem["dir_entity"] = "" if not func_dir else f"_dir-{func_dir}"

            # Check whether phase was written out:
            # 1. A magnitude needs to exist immediately before in the dicom info
            # 2. Magnitude and phase must have the same number of volumes
            series_id_idx, series_id_name = s.series_id.split("-", 1)
            prev_series_id = f"{int(series_id_idx) - 1}-{series_id_name}-{s.series_files}"
            if prev_series_id in bold_mags:
                thisitem["part_entity"] = "_part-phase"
                info[thiskey][bold_mags.index(prev_series_id)]["part_entity"] = "_part-mag"

            bold_mags.append(f"{s.series_id}-{s.series_files}")

        if thiskey is not None:
            info[thiskey].append(thisitem)

    for mod, items in info.items():
        if len(items) < 2:
            continue

        info[mod] = _assign_run_on_repeat(items)

    lgr.info(f"Finished parsing:\n{info}")

    return info


def _assign_run_on_repeat(modality_items):
    """
    Assign run IDs for repeated inputs for a given modality.

    Examples
    --------
    >>> _assign_run_on_repeat([
    ...     {"item": "discard1", "acq": "bold", "dir": "PA"},
    ...     {"item": "discard2", "acq": "bold", "dir": "AP"},
    ...     {"item": "discard3", "acq": "bold", "dir": "PA"},
    ... ])  # doctest: +NORMALIZE_WHITESPACE
    [{'item': 'discard1', 'acq': 'bold', 'dir': 'PA', 'run_entity': '_run-1'},
     {'item': 'discard2', 'acq': 'bold', 'dir': 'AP'},
     {'item': 'discard3', 'acq': 'bold', 'dir': 'PA', 'run_entity': '_run-2'}]

    >>> _assign_run_on_repeat([
    ...     {"item": "discard1", "acq": "bold", "dir": "PA"},
    ...     {"item": "discard2", "acq": "bold", "dir": "AP"},
    ...     {"item": "discard3", "acq": "bold", "dir": "PA"},
    ...     {"item": "discard4", "acq": "bold", "dir": "AP"},
    ... ])  # doctest: +NORMALIZE_WHITESPACE
    [{'item': 'discard1', 'acq': 'bold', 'dir': 'PA', 'run_entity': '_run-1'},
     {'item': 'discard2', 'acq': 'bold', 'dir': 'AP', 'run_entity': '_run-1'},
     {'item': 'discard3', 'acq': 'bold', 'dir': 'PA', 'run_entity': '_run-2'},
     {'item': 'discard4', 'acq': 'bold', 'dir': 'AP', 'run_entity': '_run-2'}]

    >>> _assign_run_on_repeat([
    ...     {"item": "discard1", "acq": "bold", "dir": "PA", "run": "1"},
    ...     {"item": "discard2", "acq": "bold", "dir": "AP"},
    ...     {"item": "discard3", "acq": "bold", "dir": "PA", "run": "2"},
    ... ])  # doctest: +NORMALIZE_WHITESPACE
    [{'item': 'discard1', 'acq': 'bold', 'dir': 'PA', 'run': '1'},
     {'item': 'discard2', 'acq': 'bold', 'dir': 'AP'},
     {'item': 'discard3', 'acq': 'bold', 'dir': 'PA', 'run': '2'}]

    >>> _assign_run_on_repeat([
    ...     {"item": "discard1", "acq": "bold", "dir": "PA", "run_entity": "_run-1"},
    ...     {"item": "discard2", "acq": "bold", "dir": "AP"},
    ...     {"item": "discard3", "acq": "bold", "dir": "PA", "run_entity": "_run-2"},
    ... ])  # doctest: +NORMALIZE_WHITESPACE
    [{'item': 'discard1', 'acq': 'bold', 'dir': 'PA', 'run_entity': '_run-1'},
     {'item': 'discard2', 'acq': 'bold', 'dir': 'AP'},
     {'item': 'discard3', 'acq': 'bold', 'dir': 'PA', 'run_entity': '_run-2'}]

    >>> _assign_run_on_repeat([
    ...     {"item": "discard1", "acq": "bold", "dir": "PA", "part_entity": "_part-mag"},
    ...     {"item": "discard2", "acq": "bold", "dir": "PA", "part_entity": "_part-phase"},
    ...     {"item": "discard3", "acq": "bold", "dir": "AP", "part_entity": "_part-mag"},
    ...     {"item": "discard4", "acq": "bold", "dir": "AP", "part_entity": "_part-phase"},
    ... ])  # doctest: +NORMALIZE_WHITESPACE
    [{'item': 'discard1', 'acq': 'bold', 'dir': 'PA', 'part_entity': '_part-mag'},
     {'item': 'discard2', 'acq': 'bold', 'dir': 'PA', 'part_entity': '_part-phase'},
     {'item': 'discard3', 'acq': 'bold', 'dir': 'AP', 'part_entity': '_part-mag'},
     {'item': 'discard4', 'acq': 'bold', 'dir': 'AP', 'part_entity': '_part-phase'}]

    """
    modality_items = modality_items.copy()

    str_patterns = [
        "_".join([f"{s[0]}-{s[1]}" for s in item.items() if s[0] != "item"])
        for item in modality_items
    ]
    strcount = Counter(str_patterns)

    for string, count in strcount.items():
        if count < 2:
            continue

        runid = 1

        for index, item_string in enumerate(str_patterns):
            if string == item_string:
                modality_items[index].update(
                    {
                        "run_entity": f"_run-{runid}",
                    }
                )
                runid += 1

    return modality_items
