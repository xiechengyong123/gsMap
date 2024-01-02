
workdir: '/storage/yangjianLab/chenwenhao/projects/202312_GPS/data/GPS_test/Nature_Neuroscience_2021/snake_workdir'
sample_name = "Cortex_151507"
chrom = "all"
rule test_run:
    input:
        f'{sample_name}/generate_ldscore/{sample_name}_generate_ldscore_{chrom}.done'

rule find_latent_representations:
    input:
        hdf5_path = "/storage/yangjianLab/songliyang/SpatialData/Data/Brain/Human/Nature_Neuroscience_2021/processed/h5ad/Cortex_151507.h5ad"
    output:
        hdf5_output = '{sample_name}/find_latent_representations/{sample_name}_add_latent.h5ad'
    params:
        annotation = "layer_guess",
        type = "count",
        epochs = 300,
        feat_hidden1 = 256,
        feat_hidden2 = 128,
        feat_cell = 3000,
        gcn_hidden1 = 64,
        gcn_hidden2 = 30,
        p_drop = 0.1,
        gcn_lr = 0.001,
        gcn_decay = 0.01,
        n_neighbors = 11,
        label_w = 1,
        rec_w = 1,
        input_pca = True,
        n_comps = 300,
        weighted_adj = False,
        nheads = 3,
        var = False,
        convergence_threshold = 1e-4,
        hierarchically = False
    shell:
        """
        GPS run_find_latent_representations --input_hdf5_path {input.hdf5_path}  --output_hdf5_path {output.hdf5_output} --sample_name {wildcards.sample_name} --annotation {params.annotation} --type {params.type} --epochs {params.epochs} --feat_hidden1 {params.feat_hidden1} --feat_hidden2 {params.feat_hidden2} --feat_cell {params.feat_cell} --gcn_hidden1 {params.gcn_hidden1} --gcn_hidden2 {params.gcn_hidden2} --p_drop {params.p_drop} --gcn_lr {params.gcn_lr} --gcn_decay {params.gcn_decay} --n_neighbors {params.n_neighbors} --label_w {params.label_w} --rec_w {params.rec_w}
        """


rule latent_to_gene:
    input:
        hdf5_with_latent_path = rules.find_latent_representations.output.hdf5_output
    output:
        feather_path = '{sample_name}/latent_to_gene/{sample_name}_gene_marker_score.feather'
    params:
        method = "rank",
        latent_representation = "latent_GVAE",
        num_neighbour = 51,
        num_neighbour_spatial = 101,
        num_processes = 4,
        fold = 1.0,
        pst = 0.2,
        species = None,
        gs_species = None,
        gM_slices = None,
        annotation = "layer_guess",
        type = "count"
    shell:
        """
        # python latent_to_gene.py --input_hdf5_with_latent_path {input.hdf5_with_latent_path} --sample_name {wildcards.sample_name} --output_feather_path {output.feather_path} --annotation {params.annotation} --type {params.type} --method {params.method} --latent_representation {params.latent_representation} --num_neighbour {params.num_neighbour} --num_neighbour_spatial {params.num_neighbour_spatial} --num_processes {params.num_processes} --fold {params.fold} --pst {params.pst} --species {params.species} --gs_species {params.gs_species} --gM_slices {params.gM_slices}
        GPS run_latent_to_gene --input_hdf5_with_latent_path {input.hdf5_with_latent_path} --sample_name {wildcards.sample_name} --output_feather_path {output.feather_path} --annotation {params.annotation} --type {params.type} --method {params.method} --latent_representation {params.latent_representation} --num_neighbour {params.num_neighbour} --num_neighbour_spatial {params.num_neighbour_spatial} --num_processes {params.num_processes} --fold {params.fold} --pst {params.pst} --species {params.species} --gs_species {params.gs_species} --gM_slices {params.gM_slices}
        """

rule generate_ldscore:
    input:
        mkscore_feather_file = rules.latent_to_gene.output.feather_path
    output:
        done = '{sample_name}/generate_ldscore/{sample_name}_generate_ldscore_{chrom}.done'
    params:
        ld_score_save_dir ='{sample_name}/generate_ldscore',
        chrom = "all",
        gtf_file = "/storage/yangjianLab/songliyang/ReferenceGenome/GRCh37/gencode.v39lift37.annotation.gtf",
        bfile_root = "/storage/yangjianLab/sharedata/LDSC_resource/1000G_EUR_Phase3_plink/1000G.EUR.QC",
        keep_snp_root = "/storage/yangjianLab/sharedata/LDSC_resource/hapmap3_snps/hm",
        window_size = 50000,
        spots_per_chunk = 10000,
        ld_wind = 1,
        ld_unit = "CM"
    shell:
        """
        # python generate_ldscore.py --sample_name {wildcards.sample_name} --chrom {params.chrom} --ldscore_save_dir {params.ld_score_save_dir} --gtf_file {params.gtf_file} --mkscore_feather_file {input.mkscore_feather_file} --bfile_root {params.bfile_root} --keep_snp_root {params.keep_snp_root} --window_size {params.window_size} --spots_per_chunk {params.spots_per_chunk} --ld_wind {params.ld_wind} --ld_unit {params.ld_unit}
        GPS run_generate_ldscore --sample_name {wildcards.sample_name} --chrom {params.chrom} --ldscore_save_dir {params.ld_score_save_dir} --gtf_file {params.gtf_file} --mkscore_feather_file {input.mkscore_feather_file} --bfile_root {params.bfile_root} --keep_snp_root {params.keep_snp_root} --window_size {params.window_size} --spots_per_chunk {params.spots_per_chunk} --ld_wind {params.ld_wind} --ld_unit {params.ld_unit}
        """
