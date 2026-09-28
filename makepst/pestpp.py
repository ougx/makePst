"""PEST++ `++` options: what PEST++ accepts, and a check of a control file's options against it.

The registry is read off PEST++ 5.2.29 (usgs/pestpp develop, commit ad8428d, 2026-07-09), from
PestppOptions::assign_value_by_key and the ies / da / mou / sqp functions it falls through to in
src/libs/pestpp_common/pest_data_structs.cpp. What that code does on start-up decides the severities:

* a known option whose value does not convert (an integer option given 2.5, a boolean given "yes") is
  "not accepted" and PEST++ stops - an error here;
* the same option twice, directly or through an alias (ies_par_en and ies_parameter_ensemble), is a
  duplicate and PEST++ stops - an error here;
* an option PEST++ does not know also stops it, unless ++forgive_unknown_args(true). Here it is only a
  warning, and the option is kept: a newer PEST++ than this registry may know it, and makePst never drops
  what the user wrote.

`++` lines go to PestppOptions only. Control variables (noptmax, maxsing, ...) are accepted in a
version-2 file's `* control data keyword` section, not as `++` options.
"""
import difflib
import re

from .sections import FIELD_SECTION

PESTPP_REGISTRY_VERSION = '5.2.29'

_TYPES = {
    int: '''
        max_n_super n_iter_base n_iter_super max_super_frz_iter max_run_fail max_reg_iter glm_num_reals sweep_chunk
        de_pop_size de_max_gen opt_stack_size opt_recalc_fosm_every gsa_morris_p gsa_morris_r gsa_sobol_samples
        ensemble_output_precision panther_agent_no_ping_timeout_secs random_seed num_tpl_ins_threads
        panther_master_timeout_milliseconds panther_master_echo_interval_milliseconds panther_ping_interval_secs
        sqp_num_reals sqp_num_refined_search_pts sqp_subset_size sqp_cma_parent_num sqp_max_consec_infeas_ies
        sqp_save_cov_every sqp_reset_hessian_every sqp_seek_feas_max_iter ies_subset_size ies_verbose_level
        ies_num_reals ies_num_threads da_hotstart_cycle da_stop_cycle mou_population_size mou_max_archive_size
        mou_verbose_level mou_save_population_every mou_max_nn_search mou_infill_size mou_resample_every
        mou_simplex_reflections''',
    float: '''
        super_eigthresh super_relparmax overdue_resched_fac overdue_giveup_fac overdue_giveup_minutes
        yamr_poll_interval de_f de_cr opt_risk opt_iter_derinc_fac opt_iter_tol gsa_morris_delta par_sigma_range
        sqp_filter_tol sqp_working_set_tol sqp_cma_reinflation_factor sqp_cma_c1 sqp_cma_cmu sqp_cma_cc
        sqp_max_reinflation_cond_num sqp_scale_down_factor sqp_hess_max_cond_num sqp_viol_pad sqp_risk
        sqp_powell_damping_factor ies_init_lam ies_reg_factor ies_bad_phi ies_bad_phi_sigma ies_accept_phi_fac
        ies_lambda_inc_fac ies_lambda_dec_fac ies_autoadaloc_sigma_dist ies_pdc_sigma_distance ies_mda_init_fac
        ies_mda_dec_fac ies_multimodal_alpha ies_multimodal_weight_exponent ies_multimodal_phi_weight
        mou_crossover_probability mou_mutation_probability mou_de_f mou_pso_omega mou_pso_alpha mou_pso_rramp
        mou_pso_rfit mou_pso_vmax_factor mou_hypervolume_extreme mou_ppd_beta mou_fit_gamma mou_fit_epsilon''',
    bool: '''
        iteration_summary der_forgive uncertainty glm_accept_mc_phi glm_rebase_super sweep_forgive sweep_base_run
        sweep_include_regul_phi tie_by_group jac_scale glm_debug_der_fail glm_debug_lamb_fail glm_debug_real_fail
        glm_debug_high_2nd_iter_phi glm_hp_lambdas glm_panther_lambdas de_dither_f opt_coin_log opt_skip_final
        opt_std_weights opt_include_bnd_pi gsa_morris_pooled_obs gsa_morris_obs_sen enforce_tied_bounds
        debug_parse_only panther_agent_restart_on_error glm_iter_mc panther_debug_loop debug_check_par_en_consistency
        panther_agent_freeze_on_fail save_all_runs check_tplins fill_tpl_zeros tpl_force_decimal forgive_unknown_args
        panther_echo panther_persistent_workers sqp_update_hessian sqp_cma_stepsize_control sqp_enforce_bounds
        sqp_use_ensemble_approx_hessian sqp_rescale_search_dir sqp_debug_enable_constraint_weighted_jco
        sqp_debug_hessian sqp_debug_cma sqp_debug_stosag_grad sqp_use_ies_infeas ies_use_approximate_solution
        ies_use_prior_scaling ies_include_base ies_use_empirical_prior ies_group_draws
        ies_enforce_bounds ies_save_binary ies_save_lambda_en ies_debug_fail_subset ies_debug_fail_remainder
        ies_debug_bad_phi ies_debug_upgrade_only ies_debug_high_subset_phi ies_debug_high_upgrade_phi ies_csv_by_reals
        ies_autoadaloc ies_enforce_chglim ies_no_noise ies_drop_conflicts ies_save_rescov ies_use_mda
        ies_upgrades_in_memory ies_ordered_binary ies_localizer_forgive_missing ies_phi_factors_by_real
        ies_update_by_reals save_dense ies_use_phi_lambda_iters da_use_simulated_states mou_risk_objective
        mou_simplex_mutation mou_use_multigen_population mou_shuffle_fixed_pars mou_debug_dv_handling''',
    str: '''
        parcov obscov base_jacobian hotstart_resfile condor_submit_file sweep_parameter_csv_file sweep_output_csv_file
        moea_name opt_obj_func opt_par_stack opt_obs_stack gsa_method gsa_sobol_par_dist additional_ins_delimiters
        sqp_dv_en sqp_restart_obs_en sqp_search_method sqp_solve_method sqp_cma_bound_handling
        sqp_hessian_update_method ies_par_en ies_obs_en ies_restart_parameter_ensemble
        ies_restart_observation_ensemble ies_weights_ensemble ies_localizer ies_localize_how
        ies_center_on ies_loc_type ies_phi_factor_file ies_run_realname da_parameter_cycle_table
        da_observation_cycle_table da_weight_cycle_table da_noptmax_schedule mou_generator mou_dv_population_file
        mou_obs_population_restart_file opt_chance_points mou_env_selector mou_mating_selector
        mou_pso_dv_bound_handling mou_outer_repo_obs_file mou_resample_command mou_population_schedule
        opt_chance_schedule
        svd_pack glm_normal_form global_opt opt_direction ies_subset_how''',
    list[float]: '''
        lambdas lambda_scale_fac sqp_alpha_mults ies_lambda_mults ies_reinflate_factor mou_pso_social_const
        mou_pso_cognitive_const mou_pso_inertia mou_simplex_factors''',
    list[int]: 'ies_n_iter_mean ies_reinflate_num_reals',
    list[str]: '''
        predictions opt_dec_var_groups opt_ext_var_groups opt_constraint_groups panther_transfer_on_finish
        panther_transfer_on_fail ies_autoadaloc_indicator_pars mou_objectives''',
}

# Constraints beyond the type. Only what PEST++ itself enforces or what cannot be meaningful; an option
# without an entry here is not a claim that every value of its type is sensible.
_CONSTRAINTS = {
    'svd_pack': {'choices': ('redsvd', 'eigen', 'jacobi', 'propack')},
    'glm_normal_form': {'choices': ('diag', 'ident', 'prior', 'hp')},
    'global_opt': {'choices': ('de', 'moea')},
    'opt_direction': {'choices': ('max', 'min')},
    'ies_subset_how': {'choices': ('first', 'last', 'random', 'phi_based')},
    'ies_num_reals': {'min': 1},
    'sqp_num_reals': {'min': 1},
    'mou_population_size': {'min': 1},
    'num_tpl_ins_threads': {'min': 1},
    'ensemble_output_precision': {'min': 1},
    'ies_reg_factor': {'min': 0},
}

# alias -> the name PEST++ stores it under; giving both is a duplicate
ALIASES = {
    'super_eigthres': 'super_eigthresh', 'forecasts': 'predictions', 'parameter_covariance': 'parcov',
    'parcov_filename': 'parcov', 'observation_covariance': 'obscov', 'obscov_filename': 'obscov',
    'base_jacobian_filename': 'base_jacobian', 'panther_poll_interval': 'yamr_poll_interval',
    'sweep_par_csv': 'sweep_parameter_csv_file', 'sweep_parameter_file': 'sweep_parameter_csv_file',
    'sweep_obs_csv': 'sweep_output_csv_file', 'sweep_output_file': 'sweep_output_csv_file',
    'opt_objective_function': 'opt_obj_func', 'opt_decision_variable_groups': 'opt_dec_var_groups',
    'opt_external_variable_groups': 'opt_ext_var_groups', 'opt_recalc_chance_every': 'opt_recalc_fosm_every',
    'parse_only': 'debug_parse_only', 'rand_seed': 'random_seed', 'ies_parameter_ensemble': 'ies_par_en',
    'ies_observation_ensemble': 'ies_obs_en', 'ies_restart_par_en': 'ies_restart_parameter_ensemble',
    'ies_restart_obs_en': 'ies_restart_observation_ensemble', 'ies_weights_en': 'ies_weights_ensemble',
    'ies_weight_ensemble': 'ies_weights_ensemble', 'ies_weight_en': 'ies_weights_ensemble',
    'ies_use_approx': 'ies_use_approximate_solution', 'ies_initial_lambda': 'ies_init_lam',
    'ies_reg_fac': 'ies_reg_factor', 'ies_add_base': 'ies_include_base', 'save_binary': 'ies_save_binary',
    'ies_save_lambda_ensembles': 'ies_save_lambda_en', 'ies_localization_type': 'ies_loc_type',
    'ies_localizer_forgive_extra': 'ies_localizer_forgive_missing', 'ies_n_iter_reinflate': 'ies_n_iter_mean',
}

# accepted, then ignored with a message on screen
DEPRECATED = ('upgrade_augment', 'upgrade_bounds', 'auto_norm', 'mat_inv')

# which tool reads an option, from its prefix; anything else is read by every PEST++ program that needs it
_PROGRAMS = {'ies_': ('pestpp-ies', 'pestpp-da'), 'da_': ('pestpp-da',), 'glm_': ('pestpp-glm',),
             'mou_': ('pestpp-mou',), 'opt_': ('pestpp-opt', 'pestpp-mou'), 'sqp_': ('pestpp-sqp',),
             'gsa_': ('pestpp-sen',), 'sweep_': ('pestpp-swp',)}

PESTPP_OPTIONS = {}
for _type, _names in _TYPES.items():
    for _name in _names.split():
        PESTPP_OPTIONS[_name] = {'type': _type, **_CONSTRAINTS.get(_name, {}),
                                 'programs': next((p for k, p in _PROGRAMS.items() if _name.startswith(k)), None)}
for _name in DEPRECATED:
    PESTPP_OPTIONS[_name] = {'type': str, 'deprecated': True, 'programs': None}
del _type, _names, _name

_INT = re.compile(r'^[-+]?\d+$')
_FLOAT = re.compile(r'^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$')     # C++ stream syntax: no Fortran 1d-3
_PLURAL = {int: 'integers', float: 'numbers'}


def canonical(name):
    """The name PEST++ files an option under, or None when this registry does not know it.

    pestpp-da accepts every ies_ option as da_ (da_num_reals is ies_num_reals).
    """
    name = name.lower()
    name = ALIASES.get(name, name)
    if name in PESTPP_OPTIONS:
        return name
    if name.startswith('da_'):
        ies = ALIASES.get('ies_' + name[3:], 'ies_' + name[3:])
        if ies in PESTPP_OPTIONS:
            return ies
    return None


def _scalar_problem(kind, text):
    if kind is int:
        return None if _INT.match(text) else 'an integer'
    if kind is float:
        return None if _FLOAT.match(text) else 'a number'
    if kind is bool:
        # PEST++ reads true/false, or an integer (0 is false). Anything else starting with t or f (t, yes, f)
        # is read as false without complaint, which is worse than an error.
        return None if text.lower() in ('true', 'false') or _INT.match(text) else 'true or false'
    return None


def value_problem(name, value):
    """Why PEST++ would not accept `value` for option `name` (a known one), or None."""
    spec = PESTPP_OPTIONS[canonical(name)]
    text = str(value).strip()
    if text == '':
        return 'has no value'
    kind = spec['type']
    if getattr(kind, '__origin__', None) is list:
        item = kind.__args__[0]
        parts = [p.strip() for p in re.split(r'[,\s]+', text) if p.strip()]
        if any(_scalar_problem(item, p) for p in parts):
            return f'must be a comma-separated list of {_PLURAL[item]} (is "{text}")'
        numbers = [float(p) for p in parts] if item in _PLURAL else []
    else:
        need = _scalar_problem(kind, text)
        if need:
            return f'must be {need} (is "{text}")'
        numbers = [float(text)] if kind in _PLURAL else []
    if 'choices' in spec and text.lower() not in spec['choices']:
        return f'must be one of {"/".join(spec["choices"])} (is "{text}")'
    if 'min' in spec and any(x < spec['min'] for x in numbers):
        return f'must be {spec["min"]} or greater (is "{text}")'
    return None


def _true(text):
    """PEST++'s reading of a boolean value."""
    text = str(text).strip().lower()
    return text == 'true' or bool(_INT.match(text)) and int(text) != 0


def check_pestpp(pst):
    """Findings for a Pst's `++` options: unknown (warning, kept), bad value or duplicate (error)."""
    from .checks import Finding
    out = []
    first = {}                                  # canonical name -> (key as written, where)
    count = {}
    forgiving = any(k.lower() == 'forgive_unknown_args' and _true(v) for k, v in pst.pestpp)
    known = sorted(set(PESTPP_OPTIONS) | set(ALIASES))
    for key, value in pst.pestpp:
        low = key.lower()
        n = count[low] = count.get(low, -1) + 1
        places = getattr(pst, 'pestpp_where', {}).get(low, [])
        where = places[n] if n < len(places) else 'pestpp options'
        name = canonical(low)
        ident = name or low
        if ident in first:
            first_key, first_where = first[ident]
            via = '' if first_key.lower() == low else f' as "{first_key}"'
            out.append(Finding('error', where, f'++{key} is already set{via} at {first_where}: PEST++ stops on a '
                                               f'duplicate option, even through an alias'))
            continue
        first[ident] = (key, where)
        if name is None:
            if low in FIELD_SECTION:
                msg = (f'"{key}" is a control variable, not a PEST++ option: PEST++ does not accept it as '
                       f'++{key}; set it in the control data instead')
            else:
                close = difflib.get_close_matches(low, known, n=1, cutoff=0.75)
                msg = f'unknown PEST++ option "{key}"' + (f'; did you mean "{close[0]}"?' if close else '')
                msg += (' (kept; ++forgive_unknown_args(true) lets PEST++ carry on)' if forgiving else
                        f' (kept; PEST++ {PESTPP_REGISTRY_VERSION} stops on options it does not accept, a newer '
                        f'build may know it)')
            out.append(Finding('info' if forgiving else 'warning', where, msg))
            continue
        problem = value_problem(low, value)
        if problem:
            out.append(Finding('error', where, f'++{key} {problem}'))
        elif PESTPP_OPTIONS[name].get('deprecated'):
            out.append(Finding('info', where, f'++{key} is deprecated; PEST++ ignores it'))
        elif name == 'svd_pack' and str(value).strip().lower() == 'propack':
            out.append(Finding('info', where, '++svd_pack(propack) is deprecated; PEST++ uses redsvd instead'))
    return out
