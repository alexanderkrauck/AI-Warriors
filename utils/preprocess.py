from utils.download import decompress_lzo_file

from dask import dataframe as dd
from dask import delayed

import numpy as np
import pandas as pd

from collections import Counter
from typing import Union, List, Tuple, Callable, Dict
import os
from os.path import join
from pathlib import Path

from .constants import COMP_DIR, is_label
from .config import load_feature_config, load_mop_config, load_compute_config, load_manifest, dump_manifest
from .compute_and_front import uncompress_and_parquetize, load_all_preprocessed_data
from .features import single_column_features, conditional_probabilities, TE_dataframe_dask, TE_get_name
from .download import shelf_directory, ensure_dir_exists

def preprocess(
        config: dict = None,
        comp_dir: Union[os.PathLike, str] = COMP_DIR
    ) -> Tuple[dd.DataFrame, dd.DataFrame, List[delayed]]:
    '''

    Parameters
    ----------
    config: dict
        configuration dictionary from the yaml file
    comp_dir: Union(os.PathLike, str)
        The location of the compressed data.

    
    Returns
    -------
        lazy dataframe that contains all of the features, can be used for more computations if desired
        delayed object that needs to be computed in an environment of choice in order for the files to be produced
        e.g.
        ddf, dump, stat_dump = preprocess()
        f = client.persist(dump, *stat_dump)  # can take multiple objects
        progress(f)
        f.compute()     # gathers the result
    '''

    # split the config file
    mop_config = load_mop_config(config)  # mode of preprocessing part
    compute_config = load_compute_config(config)   # all to do with computation
    feature_config = load_feature_config(config)     # all to do with specifications of the features that we need

    #Load default config if not specified and extract required parameters
    verbosity = mop_config['verbose']

    # unpack some of the config variables
    train_set_mode = mop_config['train_set']
    data_source = mop_config['load_from']

    #Set default directories if not specified (based on root dir)
    new_features_dir = join(comp_dir, mop_config['feature_dir'])
    prepro_dir = join(comp_dir, mop_config['preprocessed_dir'])
    stat_dir = join(comp_dir, mop_config['prepro_statistics_dir'])

    if data_source == 'comp' or data_source == "uncomp":
        uncompress_and_parquetize(comp_dir, config)

    # if we are not doing additive, then shelf the new feature directory
    if not mop_config['additive_preprocessing']:
        shelf_directory(new_features_dir)
    else:
        ensure_dir_exists(new_features_dir)
    ensure_dir_exists(stat_dir)

    # default parameters work just fine
    ddf = load_all_preprocessed_data(comp_dir=comp_dir,
                                     new_features=mop_config['additive_preprocessing'],
                                     old_features=True,
                                     mop_config=mop_config)
    other_delayed = []
    manifest = {'available_features': [],
                'TE_stats': {}} if train_set_mode else load_manifest(os.path.join(comp_dir, mop_config['manifesto']))
    original_cols = [col for col in ddf.columns]


    # first add the features as per configuration file
    for feature in feature_config['basic_features']:
        # so that we do not recompute features when in additive mode
        if feature in original_cols and mop_config['additive_preprocessing']:
            continue
            # otherwise the feature must not exist as we do not load
        cols, fun, dt, iterlevel = single_column_features[feature]
        # so that we do not attempt to preprocess the labels which we do not have in the test set
        if any([is_label(col) for col in cols]) and not train_set_mode:
            continue

        # apply to the corresponding iterlevel
        if iterlevel == 1:
            f_series = ddf[cols].apply(fun, meta=(feature, dt))
        elif iterlevel == 2:
            f_series = ddf[cols].to_frame().apply(fun, axis=1, meta=(feature, dt))
        elif iterlevel == 3:
            f_series = ddf[cols].map_partitions(fun, meta=(feature, dt))
        else:
            raise ValueError("Wrong iterlevel.")

        # join with the dataset on index
        ddf = dd.merge(ddf,
                       f_series,
                       how='inner',     # TODO: investigate if this should be changed to left
                       left_index=True,
                       right_index=True)

        manifest['available_features'].append(feature)

    #factorize features with small cardinality
    # Path(new_features_dir).mkdir(exist_ok=True, parents=True)
    # with get_dask_compute_environment(config) as client:
    #     for col in config["low_cardinality_rehash_features"]:
    #         tmp_col = f'{col}_encode'
    #         fut_tmp = ddf[col].unique()
    #         fut_tmp = client.persist(fut_tmp)
    #         if verbosity > 0:
    #             progress(fut_tmp)
    #         tmp = fut_tmp.compute()
    #         tmp = tmp.to_frame().reset_index()
    #         tmp.columns = [i if i!="index" else tmp_col for i in tmp.columns]
    #         ddf = ddf.merge(tmp, on=col, how='left')
    #         ddf[tmp_col] = ddf[tmp_col].astype('uint8')
    #         mapping_output = join(new_features_dir, f"{col}_mapping.csv")
    #         if verbosity >= 1: print(f"Outputing mapping for {col} to {mapping_output}")
    #         tmp[[tmp_col, col]].to_csv(mapping_output)


    # then add the TEs as per configuration
    for te_feature, te_targets in feature_config['TE_features'].items():
        for te_target in te_targets:
            if train_set_mode:
                # for the training set generate the counts and means and lazily dump them into corresponding files
                ddf, cnm = TE_dataframe_dask(ddf, te_feature, te_target)       # already joins in the function

                fname = TE_get_name(te_feature, te_target)
                stat_path = os.path.join(stat_dir, fname)
                ensure_dir_exists(stat_path)
                other_delayed.append(cnm.to_parquet(stat_path, compute=False))

                manifest['TE_stats'][fname] = fname
            else:
                # load the cnm if preprocessing targets
                fname = TE_get_name(te_feature, te_target)
                stat_path = os.path.join(stat_dir, manifest['TE_stats'][fname])
                cnm = dd.read_parquet(stat_path)
                # use them to compute TEs for the dataframe
                ddf, cnm = TE_dataframe_dask(ddf, te_feature, te_target, counts_and_means=cnm)
                # already joins in the function

    new_feature_columns = [col for col in ddf.columns if col not in original_cols]
    if verbosity >= 1:
        print("The following preprocessed columns can be dumped: ", new_feature_columns)
    delayed_dump = ddf[new_feature_columns].to_parquet(new_features_dir, compute=False)

    # save the manifest if in train mode
    if train_set_mode:
        dump_manifest(mop_config["manifesto"], manifest)

    if verbosity >= 1:
        print("Feature generation ready, manifest dumped, associated delayed objects: {}\n\n{}.".format(ddf, delayed_dump))

    # can compute just the ddf for the dataframe itself or the other two if full dump is needed
    return ddf, delayed_dump, other_delayed

