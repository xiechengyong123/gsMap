import os
import zarr
import numpy as np
import pandas as pd

import argparse
import logging
import multiprocessing
from collections import defaultdict
from pathlib import Path

from scipy.stats import norm
from tqdm.contrib.concurrent import process_map

import gsMap.jackknife as jk
from gsMap.config import add_spatial_ldsc_args, SpatialLDSCConfig
from gsMap.regression_read import _read_sumstats, _read_w_ld, _read_ref_ld_v2, _read_M_v2

logger = logging.getLogger(__name__)


# %%
def _coef_new(jknife):
    # return coef[0], coef_se[0], z[0]]
    # est_ = jknife.est[0, 0] / Nbar
    est_ = jknife.jknife_est[0, 0] / Nbar
    se_ = jknife.jknife_se[0, 0] / Nbar
    return est_, se_


def append_intercept(x):
    n_row = x.shape[0]
    intercept = np.ones((n_row, 1))
    x_new = np.concatenate((x, intercept), axis=1)
    return x_new


def filter_sumstats_by_chisq(sumstats, chisq_max):
    before_len = len(sumstats)
    if chisq_max is None:
        chisq_max = max(0.001 * sumstats.N.max(), 80)
        logger.info(f'No chi^2 threshold provided, using {chisq_max} as default')
    sumstats['chisq'] = sumstats.Z ** 2
    sumstats = sumstats[sumstats.chisq < chisq_max]
    after_len = len(sumstats)
    if after_len < before_len:
        logger.info(f'Removed {before_len - after_len} SNPs with chi^2 > {chisq_max} ({after_len} SNPs remain)')
    else:
        logger.info(f'No SNPs removed with chi^2 > {chisq_max} ({after_len} SNPs remain)')
    return sumstats


def aggregate(y, x, N, M, intercept=1):
    num = M * (np.mean(y) - intercept)
    denom = np.mean(np.multiply(x, N))
    return num / denom


def weights(ld, w_ld, N, M, hsq, intercept=1):
    M = float(M)
    hsq = np.clip(hsq, 0.0, 1.0)
    ld = np.maximum(ld, 1.0)
    w_ld = np.maximum(w_ld, 1.0)
    c = hsq * N / M
    het_w = 1.0 / (2 * np.square(intercept + np.multiply(c, ld)))
    oc_w = 1.0 / w_ld
    w = np.multiply(het_w, oc_w)
    return w


def jackknife_for_processmap(spot_id):
    # calculate the initial weight for each spot
    spot_spatial_annotation = spatial_annotation[:, spot_id]
    spot_x_tot_precomputed = spot_spatial_annotation + ref_ld_baseline_column_sum
    initial_w = (
        get_weight_optimized(sumstats, x_tot_precomputed=spot_x_tot_precomputed,
                             M_tot=10000, w_ld=w_ld_common_snp, intercept=1)
        .astype(np.float32)
        .reshape((-1, 1)))

    # apply the weight to baseline annotation, spatial annotation and CHISQ
    initial_w_scaled = initial_w / np.sum(initial_w)
    baseline_annotation_spot = baseline_annotation * initial_w_scaled
    spatial_annotation_spot = spot_spatial_annotation.reshape((-1, 1)) * initial_w_scaled
    CHISQ = sumstats.chisq.values.reshape((-1, 1))
    y = CHISQ * initial_w_scaled

    # run the jackknife
    x_focal = np.concatenate((spatial_annotation_spot,
                              baseline_annotation_spot), axis=1)
    try:
        jknife = jk.LstsqJackknifeFast(x_focal, y, n_blocks)
        # LinAlgError
    except np.linalg.LinAlgError as e:
        logger.warning(f'LinAlgError: {e}')
        return np.nan, np.nan
    return _coef_new(jknife)


# Updated function
def get_weight_optimized(sumstats, x_tot_precomputed, M_tot, w_ld, intercept=1):
    tot_agg = aggregate(sumstats.chisq, x_tot_precomputed, sumstats.N, M_tot, intercept)
    initial_w = weights(x_tot_precomputed, w_ld.LD_weights.values, sumstats.N.values, M_tot, tot_agg, intercept)
    initial_w = np.sqrt(initial_w)
    return initial_w


def _preprocess_sumstats(trait_name, sumstat_file_path, baseline_and_w_ld_common_snp: pd.Index, chisq_max=None):
    # Load the gwas summary statistics
    sumstats = _read_sumstats(fh=sumstat_file_path, alleles=False, dropna=False)
    sumstats.set_index('SNP', inplace=True)
    sumstats = sumstats.astype(np.float32)
    sumstats = filter_sumstats_by_chisq(sumstats, chisq_max)

    # NB: The intersection order is essential for keeping the same order of SNPs by its BP location
    common_snp = baseline_and_w_ld_common_snp.intersection(sumstats.index)
    if len(common_snp) < 200000:
        logger.warning(f'WARNING: number of SNPs less than 200k; for {trait_name} this is almost always bad.')

    sumstats = sumstats.loc[common_snp]

    # get the common index position of baseline_and_w_ld_common_snp for quick access
    sumstats['common_index_pos'] = pd.Index(baseline_and_w_ld_common_snp).get_indexer(sumstats.index)
    return sumstats


def _get_sumstats_from_sumstats_dict(sumstats_config_dict: dict, baseline_and_w_ld_common_snp: pd.Index,
                                     chisq_max=None):
    # first validate if all sumstats file exists
    logger.info('Validating sumstats files...')
    for trait_name, sumstat_file_path in sumstats_config_dict.items():
        if not os.path.exists(sumstat_file_path):
            raise FileNotFoundError(f'{sumstat_file_path} not found')
    # then load all sumstats
    sumstats_cleaned_dict = {}
    for trait_name, sumstat_file_path in sumstats_config_dict.items():
        sumstats_cleaned_dict[trait_name] = _preprocess_sumstats(trait_name, sumstat_file_path,
                                                                 baseline_and_w_ld_common_snp, chisq_max)
    logger.info('cleaned sumstats loaded')
    return sumstats_cleaned_dict


def _get_sumstats_with_common_snp_from_sumstats_dict(sumstats_config_dict: dict, baseline_and_w_ld_common_snp: pd.Index,
                                                     chisq_max=None):
    # first validate if all sumstats file exists
    logger.info('Validating sumstats files...')
    for trait_name, sumstat_file_path in sumstats_config_dict.items():
        if not os.path.exists(sumstat_file_path):
            raise FileNotFoundError(f'{sumstat_file_path} not found')
    # then load all sumstats
    sumstats_cleaned_dict = {}
    for trait_name, sumstat_file_path in sumstats_config_dict.items():
        sumstats_cleaned_dict[trait_name] = _preprocess_sumstats(trait_name, sumstat_file_path,
                                                                 baseline_and_w_ld_common_snp, chisq_max)
    # get the common snps among all sumstats
    common_snp_among_all_sumstats = None
    for trait_name, sumstats in sumstats_cleaned_dict.items():
        if common_snp_among_all_sumstats is None:
            common_snp_among_all_sumstats = sumstats.index
        else:
            common_snp_among_all_sumstats = common_snp_among_all_sumstats.intersection(sumstats.index)

    # filter the common snps among all sumstats
    for trait_name, sumstats in sumstats_cleaned_dict.items():
        sumstats_cleaned_dict[trait_name] = sumstats.loc[common_snp_among_all_sumstats]

    logger.info(f'!Common SNPs among all sumstats: {len(common_snp_among_all_sumstats)}')
    return sumstats_cleaned_dict, common_snp_among_all_sumstats


def run_spatial_ldsc(config: SpatialLDSCConfig):
    global spatial_annotation, baseline_annotation, n_blocks, Nbar, sumstats, ref_ld_baseline_column_sum, w_ld_common_snp
    # config
    n_blocks = config.n_blocks
    sample_name = config.sample_name

    print(f'------Running Spatial LDSC for {sample_name}...')
    # Load the regression weights
    w_ld = _read_w_ld(config.w_file)
    w_ld_cname = w_ld.columns[1]
    w_ld.set_index('SNP', inplace=True)

    # Load the baseline annotations
    ld_file_baseline = f'{config.ldscore_input_dir}/baseline/baseline.'
    ref_ld_baseline = _read_ref_ld_v2(ld_file_baseline)
    n_annot_baseline = len(ref_ld_baseline.columns)
    M_annot_baseline = _read_M_v2(ld_file_baseline, n_annot_baseline, config.not_M_5_50)

    # common snp between baseline and w_ld
    baseline_and_w_ld_common_snp = ref_ld_baseline.index.intersection(w_ld.index)
    baseline_and_w_ld_common_snp_pos = pd.Index(ref_ld_baseline.index).get_indexer(baseline_and_w_ld_common_snp)

    # Clean the sumstats
    sumstats_cleaned_dict, common_snp_among_all_sumstats = _get_sumstats_with_common_snp_from_sumstats_dict(
        config.sumstats_config_dict, baseline_and_w_ld_common_snp,
        chisq_max=config.chisq_max)
    common_snp_among_all_sumstats_pos = ref_ld_baseline.index.get_indexer(common_snp_among_all_sumstats)

    # insure the order is monotonic
    assert pd.Series(
        common_snp_among_all_sumstats_pos).is_monotonic_increasing, 'common_snp_among_all_sumstats_pos is not monotonic increasing'

    if len(common_snp_among_all_sumstats) < 200000:
        logger.warning(
            f'!!!!! WARNING: number of SNPs less than 200k; for {sample_name} this is almost always bad. Please check the sumstats files.')

    ref_ld_baseline = ref_ld_baseline.loc[common_snp_among_all_sumstats]
    w_ld = w_ld.loc[common_snp_among_all_sumstats]

    # load additional baseline annotations
    if config.use_additional_baseline_annotation:
        ld_file_baseline_additional = f'{config.ldscore_input_dir}/additional_baseline/baseline.'
        ref_ld_baseline_additional = _read_ref_ld_v2(ld_file_baseline_additional)
        n_annot_baseline_additional = len(ref_ld_baseline_additional.columns)
        logger.info(f'{len(ref_ld_baseline_additional.columns)} additional baseline annotations loaded')
        # M_annot_baseline_additional = _read_M_v2(ld_file_baseline_additional, n_annot_baseline_additional,
        #                                             config.not_M_5_50)
        ref_ld_baseline_additional = ref_ld_baseline_additional.loc[common_snp_among_all_sumstats]
        ref_ld_baseline = pd.concat([ref_ld_baseline, ref_ld_baseline_additional], axis=1)
        del ref_ld_baseline_additional

    # Detect avalable chunk files
    all_file = os.listdir(config.ldscore_input_dir)
    total_chunk_number_found = sum('chunk' in name for name in all_file)
    print(f'Find {total_chunk_number_found} chunked files in {config.ldscore_input_dir}')

    if config.all_chunk is None:
        if config.chunk_range is not None:
            assert config.chunk_range[0] >= 1 and config.chunk_range[
                1] <= total_chunk_number_found, 'Chunk range out of bound. It should be in [1, all_chunk]'
            print(
                f'chunk range provided, using chunked files from {config.chunk_range[0]} to {config.chunk_range[1]}')
            start_chunk, end_chunk = config.chunk_range
        else:
            start_chunk, end_chunk = 1, total_chunk_number_found
    else:
        all_chunk = config.all_chunk
        print(f'using {all_chunk} chunked files by provided argument')
        print(f'\t')
        print(f'Input {all_chunk} chunked files')
        start_chunk, end_chunk = 1, all_chunk
    running_chunk_number = end_chunk - start_chunk + 1

    # Process each chunk
    output_dict = defaultdict(list)
    zarr_path = Path(config.ldscore_input_dir) / f'{config.sample_name}.ldscore.zarr'
    if config.ldscore_save_format == 'zarr':
        assert zarr_path.exists(), f'{zarr_path} not found, which is required for zarr format'
        zarr_file = zarr.open(str(zarr_path))
        spots_name = zarr_file.attrs['spot_names']

    for chunk_index in range(start_chunk, end_chunk + 1):
        print(f'------Processing chunk-{chunk_index}')
        if config.ldscore_save_format == 'feather':
            ref_ld_spatial, spatial_annotation_cnames = load_ldscore_chunk_from_feather(chunk_index,
                                                                                        common_snp_among_all_sumstats_pos,
                                                                                        config,
                                                                                        )
        elif config.ldscore_save_format == 'zarr':
            ref_ld_spatial = zarr_file.blocks[:, chunk_index - 1][common_snp_among_all_sumstats_pos]
            start_spot = (chunk_index - 1) * zarr_file.chunks[1]
            ref_ld_spatial = ref_ld_spatial.astype(np.float32, copy=False)
            spatial_annotation_cnames = spots_name[start_spot:start_spot + zarr_file.chunks[1]]

        else:
            raise ValueError(f'Invalid ld score save format: {config.ldscore_save_format}')

        # get the x_tot_precomputed matrix by adding baseline and spatial annotation
        ref_ld_baseline_column_sum = ref_ld_baseline.sum(axis=1).values
        # x_tot_precomputed = ref_ld_spatial + ref_ld_baseline_column_sum

        for trait_name, sumstats in sumstats_cleaned_dict.items():
            logger.info(f'Processing {trait_name}...')

            # filter ldscore by common snp
            # common_snp = sumstats.index
            # common_index_pos = sumstats.common_index_pos.values
            # spatial_annotation = ref_ld_spatial.iloc[common_index_pos].astype(np.float32, copy=False)
            # spatial_annotation_cnames = spatial_annotation.columns
            # baseline_annotation = ref_ld_baseline.iloc[common_index_pos].astype(np.float32, copy=False)
            # w_ld_common_snp = w_ld.iloc[common_index_pos].astype(np.float32, copy=False)
            # x_tot_precomputed_common_snp = x_tot_precomputed.iloc[common_index_pos].values

            spatial_annotation = ref_ld_spatial.astype(np.float32, copy=False)
            baseline_annotation = ref_ld_baseline.copy().astype(np.float32, copy=False)
            w_ld_common_snp = w_ld.astype(np.float32, copy=False)

            # weight the baseline annotation by N
            baseline_annotation = baseline_annotation * sumstats.N.values.reshape((-1, 1)) / sumstats.N.mean()
            # append intercept
            baseline_annotation = append_intercept(baseline_annotation)

            # Run the jackknife
            Nbar = sumstats.N.mean()
            chunk_size = spatial_annotation.shape[1]
            out_chunk = process_map(jackknife_for_processmap, range(chunk_size),
                                    max_workers=config.num_processes,
                                    chunksize=10,
                                    desc=f'LDSC chunk-{chunk_index}: {trait_name}')

            # cache the results
            out_chunk = pd.DataFrame.from_records(out_chunk,
                                                  columns=['beta', 'se', ],
                                                  index=spatial_annotation_cnames)
            # get the spots with nan
            nan_spots = out_chunk[out_chunk.isna().any(axis=1)].index
            logger.info(f'Nan spots: {nan_spots} in chunk-{chunk_index} for {trait_name}. They are removed.')
            # drop the nan
            out_chunk = out_chunk.dropna()

            out_chunk['z'] = out_chunk.beta / out_chunk.se
            out_chunk['p'] = norm.sf(out_chunk['z'])
            output_dict[trait_name].append(out_chunk)

    # Save the results
    out_dir = Path(config.ldsc_save_dir)
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o777)
    for trait_name, out_chunk_list in output_dict.items():
        out_all = pd.concat(out_chunk_list, axis=0)
        if running_chunk_number == total_chunk_number_found:
            out_file_name = out_dir / f'{sample_name}_{trait_name}.csv.gz'
        else:
            out_file_name = out_dir / f'{sample_name}_{trait_name}_chunk{start_chunk}-{end_chunk}.csv.gz'
        out_all['spot'] = out_all.index
        out_all = out_all[['spot', 'beta', 'se', 'z', 'p']]
        out_all.to_csv(out_file_name, compression='gzip', index=False)

        logger.info(f'Output saved to {out_file_name} for {trait_name}')
    logger.info(f'------Spatial LDSC for {sample_name} finished!')


def load_ldscore_chunk_from_feather(chunk_index, common_snp_among_all_sumstats_pos, config, ):
    # Load the spatial annotations for this chunk
    sample_name = config.sample_name
    ld_file_spatial = f'{config.ldscore_input_dir}/{sample_name}_chunk{chunk_index}/{sample_name}.'
    ref_ld_spatial = _read_ref_ld_v2(ld_file_spatial)
    ref_ld_spatial = ref_ld_spatial.iloc[common_snp_among_all_sumstats_pos]
    ref_ld_spatial = ref_ld_spatial.astype(np.float32, copy=False)

    spatial_annotation_cnames = ref_ld_spatial.columns
    return ref_ld_spatial.values, spatial_annotation_cnames


# %%
if __name__ == '__main__':
    # Main function of analysis
    parser = argparse.ArgumentParser(
        description="Run Spatial LD Score Regression (LDSC) analysis for GWAS and spatial transcriptomic data."
    )
    parser = add_spatial_ldsc_args(parser)
    TEST = True
    # if TEST:
    #     gwas_root = "/storage/yangjianLab/songliyang/GWAS_trait/LDSC"
    #     gwas_trait = "/storage/yangjianLab/songliyang/GWAS_trait/GWAS_Public_Use_MaxPower.csv"
    #     root = "/storage/yangjianLab/songliyang/SpatialData/Data/Brain/Human/Nature_Neuroscience_2021/processed/h5ad"
    #
    #     name = 'Cortex_151507'
    #     spe_name = name
    #     # ld_pth = f"/storage/yangjianLab/songliyang/SpatialData/Data/Brain/Human/Nature_Neuroscience_2021/annotation/{spe_name}/snp_annotation"
    #     ld_pth = f"/storage/yangjianLab/chenwenhao/projects/202312_gsMap/data/gsMap_test/Nature_Neuroscience_2021/snake_workdir/{name}/generate_ldscore"
    #     out_pth = f"/storage/yangjianLab/chenwenhao/projects/202312_gsMap/data/gsMap_test/Nature_Neuroscience_2021/snake_workdir/{name}/ldsc"
    #     gwas_file = "ADULT1_ADULT2_ONSET_ASTHMA"
    #     # Prepare the arguments list using f-strings
    #     args_list = [
    #         "--h2", f"{gwas_root}/{gwas_file}.sumstats.gz",
    #         "--w_file", "/storage/yangjianLab/sharedata/LDSC_resource/LDSC_SEG_ldscores/weights_hm3_no_hla/weights.",
    #         "--sample_name", spe_name,
    #         "--num_processes", '4',
    #         "--ldscore_input_dir", ld_pth,
    #         "--ldsc_save_dir", out_pth,
    #         '--trait_name', 'adult1_adult2_onset_asthma'
    #     ]
    #     # args = parser.parse_args(args_list)
    # else:
    #     args = parser.parse_args()
    #
    # os.chdir('/storage/yangjianLab/chenwenhao/tmp/gsMap_Height_debug')
    # TASK_ID = 16
    # spe_name = f'E{TASK_ID}.5_E1S1'
    # config = SpatialLDSCConfig(**{'all_chunk': 1,
    #                               'chisq_max': None,
    #                               # 'sumstats_file': '/storage/yangjianLab/songliyang/GWAS_trait/LDSC/GIANT_EUR_Height_2022_Nature.sumstats.gz',
    #                               'ldsc_save_dir': f'{spe_name}/ldsc_results_three_row_sum_sub_config_traits',
    #                               'ldscore_input_dir': '/storage/yangjianLab/songliyang/SpatialData/Data/Embryo/Mice/Cell_MOSTA/annotation/E16.5_E1S1/generate_ldscore_new',
    #                               'n_blocks': 200,
    #                               'not_M_5_50': False,
    #                               'num_processes': 15,
    #                               'sample_name': spe_name,
    #                               # 'trait_name': 'GIANT_EUR_Height_2022_Nature',
    #                               'sumstats_config_file': '/storage/yangjianLab/chenwenhao/projects/202312_GPS/src/GPS/example/sumstats_config_sub.yaml',
    #                               'w_file': '/storage/yangjianLab/sharedata/LDSC_resource/LDSC_SEG_ldscores/weights_hm3_no_hla/weights.'
    #                               })
    import time
    start = time.time()
    ldscore_save_format = 'feather'
    config = SpatialLDSCConfig(sample_name='Cortex_151507',
                               w_file='/storage/yangjianLab/chenwenhao/projects/202312_GPS/test/docs_test/gsMap_resource/LDSC_resource/weights_hm3_no_hla/weights.',
                               ldscore_input_dir='/storage/yangjianLab/chenwenhao/projects/202312_GPS/data/GPS_test/Nature_Neuroscience_2021/Cortex_151507/snp_annotation/test/0101/sparse',
                               ldsc_save_dir=f'/storage/yangjianLab/chenwenhao/projects/202312_GPS/data/GPS_test/Nature_Neuroscience_2021/Cortex_151507/snp_annotation/test/0101/sparse/Cortex_151507/spatial_ldsc{ldscore_save_format}',
                               disable_additional_baseline_annotation=True,
                               trait_name=None,
                               sumstats_file=None,
                               sumstats_config_file='/storage/yangjianLab/chenwenhao/projects/202312_GPS/test/docs_test/example_data/GWAS/gwas_config_absolute_path.yaml',
                               num_processes=10,
                               not_M_5_50=False,
                               n_blocks=200,
                               chisq_max=None,
                               all_chunk=None,
                               chunk_range=(1, 2),
                               ldscore_save_format=ldscore_save_format
                               )
    # config = SpatialLDSCConfig(**vars(args))
    run_spatial_ldsc(config)
    print(f'Elapsed time: {time.time() - start} using {config.num_processes} processes in {ldscore_save_format} format')
