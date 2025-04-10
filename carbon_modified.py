# Adopted from InVEST model with modifications for own applications
# === [MODIFICATIONS] ===
# Supports:
# 1. Multi-year LULC time series inputs
# 2. Extended carbon pools (deep soil carbon)
# 3. Climate impact raster modifier
# === [BEGIN MODIFIED BLOCK] ===

# Climate Impact Raster Modifier
CLIMATE_IMPACT_PATH = 'climate_impact_scenarios.tif'  # input raster (0–100 % loss)

# Multi-year LULC support
LULC_TIME_SERIES = []  # e.g., [('lulc_2020.tif', 2020), ('lulc_2030.tif', 2030)]

# Extended Carbon Pools
EXTENDED_CARBON_POOLS = [
    ('c_above', 'aboveground'),
    ('c_below', 'belowground'),
    ('c_soil', 'soil'),
    ('c_dead', 'dead matter'),
    ('c_deepsoil', 'deep soil carbon')
]

def apply_climate_impact(carbon_array, impact_raster_path):
    """Applies a % climate impact reduction to carbon array."""
    if not os.path.exists(impact_raster_path):
        return carbon_array
    impact_array = pygeoprocessing.raster_to_numpy_array(impact_raster_path)
    return carbon_array * (1 - impact_array / 100)

def process_lulc_time_series(lulc_series, carbon_pool_dict, pixel_area, climate_path=None):
    """Process list of (lulc_raster, year) pairs and return carbon totals."""
    results = []
    for lulc_path, year in lulc_series:
        LOGGER.info(f"Processing {lulc_path} for year {year}")
        lulc_array = pygeoprocessing.raster_to_numpy_array(lulc_path)
        carbon_map = numpy.zeros_like(lulc_array)
        for lulc_val, carbon_val in carbon_pool_dict.items():
            carbon_map[lulc_array == lulc_val] += carbon_val * pixel_area / 10000
        if climate_path:
            carbon_map = apply_climate_impact(carbon_map, climate_path)
        total = numpy.sum(carbon_map[carbon_map != -1.0])
        results.append((year, total))
    return results

# === [END MODIFIED BLOCK] ===

# coding=UTF-8
"""Carbon Storage and Sequestration."""
import codecs
import logging
import os
import time
from functools import reduce

from osgeo import gdal
import numpy
import pygeoprocessing
import taskgraph

from . import validation
from . import utils
from . import spec_utils
from .unit_registry import u
from .model_metadata import MODEL_METADATA
from . import gettext

LOGGER = logging.getLogger(__name__)

CARBON_OUTPUTS = {
    f"c_{pool}_{scenario}.tif": {
        "about": (
            f"Raster of {pool_name} carbon values in the {scenario_name} "
            "scenario, mapped from the Carbon Pools table to the LULC."),
        "bands": {1: {
            "type": "number",
            "units": u.metric_ton/u.pixel
        }}
    } for pool, pool_name in [
        ('above', 'aboveground'),
        ('below', 'belowground'),
        ('soil', 'soil'),
        ('dead', 'dead matter'),
        ('deepsoil', 'deep soil carbon')
    ] for scenario, scenario_name in [
        ('cur', 'current'),
        ('fut', 'future'),
        ('redd', 'REDD')
    ]
}

MODEL_SPEC = {
    "model_name": MODEL_METADATA["carbon"].model_title,
    "pyname": MODEL_METADATA["carbon"].pyname,
    "userguide": MODEL_METADATA["carbon"].userguide,
    "args_with_spatial_overlap": {
        "spatial_keys": ["lulc_cur_path", "lulc_fut_path", "lulc_redd_path"],
    },
    "args": {
        "workspace_dir": spec_utils.WORKSPACE,
        "results_suffix": spec_utils.SUFFIX,
        "n_workers": spec_utils.N_WORKERS,
        "lulc_cur_path": {
            **spec_utils.LULC,
            "projected": True,
            "projection_units": u.meter,
            "about": gettext(
                "A map of LULC for the current scenario. "
                "All values in this raster must have corresponding "
                "entries in the Carbon Pools table."),
            "name": gettext("current LULC")
        },
        "calc_sequestration": {
            "type": "boolean",
            "required": "do_valuation | do_redd",
            "about": gettext(
                "Run sequestration analysis. This requires inputs "
                "of LULC maps for both current and future "
                "scenarios. Required if REDD scenario analysis or "
                "run valuation model is selected."),
            "name": gettext("calculate sequestration")
        },
        "lulc_fut_path": {
            **spec_utils.LULC,
            "projected": True,
            "projection_units": u.meter,
            "required": "calc_sequestration",
            "about": gettext(
                "A map of LULC for the future scenario. "
                "If run valuation model is "
                "selected, this should be the reference, or baseline, future "
                "scenario against which to compare the REDD policy scenario. "
                "All values in this raster must have corresponding entries in "
                "the Carbon Pools table. Required if Calculate Sequestration "
                "is selected."),
            "name": gettext("future LULC")
        },
        "do_redd": {
            "type": "boolean",
            "required": False,
            "about": gettext(
                "Run REDD scenario analysis. This requires three "
                "LULC maps: one for the current scenario, one "
                "for the future baseline scenario, and one for the future "
                "REDD policy scenario."),
            "name": gettext("REDD scenario analysis")
        },
        "lulc_redd_path": {
            **spec_utils.LULC,
            "projected": True,
            "projection_units": u.meter,
            "required": "do_redd",
            "about": gettext(
                "A map of LULC for the REDD policy scenario. "
                "All values in this raster must have corresponding entries in "
                "the Carbon Pools table. Required if REDD Scenario Analysis "
                "is selected."),
            "name": gettext("REDD LULC")
        },
        "carbon_pools_path": {
            "type": "csv",
            "columns": {
                "lucode": spec_utils.LULC_TABLE_COLUMN,
                "c_above": {
                    "type": "number",
                    "units": u.metric_ton/u.hectare,
                    "about": gettext("Carbon density of aboveground biomass.")},
                "c_below": {
                    "type": "number",
                    "units": u.metric_ton/u.hectare,
                    "about": gettext("Carbon density of belowground biomass.")},
                "c_soil": {
                    "type": "number",
                    "units": u.metric_ton/u.hectare,
                    "about": gettext("Carbon density of soil.")},
                "c_dead": {
                    "type": "number",
                    "units": u.metric_ton/u.hectare,
                    "about": gettext("Carbon density of dead matter.")},
                'c_deepsoil': {
                    "type": "number",
                    "units": u.metric_ton/u.hectare,
                    "about": gettext("Carbon density of deep soil.")}
            },
            "index_col": "lucode",
            "about": gettext(
                "A table that maps each LULC code to carbon pool data for "
                "that LULC type."),
            "name": gettext("carbon pools")
        },
        "lulc_cur_year": {
            "expression": "float(value).is_integer()",
            "type": "number",
            "units": u.year_AD,
            "required": "do_valuation",
            "about": gettext(
                "The calendar year of the current scenario depicted in the "
                "current LULC map. Required if Run Valuation model is selected."),
            "name": gettext("current LULC year")
        },
        "lulc_fut_year": {
            "expression": "float(value).is_integer()",
            "type": "number",
            "units": u.year_AD,
            "required": "do_valuation",
            "about": gettext(
                "The calendar year of the future scenario depicted in the "
                "future LULC map. Required if Run Valuation model is selected."),
            "name": f"future LULC year"
        },
        "do_valuation": {
            "type": "boolean",
            "required": False,
            "about": gettext(
                "Calculate net present value for the future scenario, and the "
                "REDD scenario if provided, and report it in the final HTML "
                "document."),
            "name": gettext("run valuation model")
        },
        "price_per_metric_ton_of_c": {
            "type": "number",
            "units": u.currency/u.metric_ton,
            "required": "do_valuation",
            "about": gettext(
                "The present value of carbon. "
                "Required if Run Valuation model is selected."),
            "name": gettext("price of carbon")
        },
        "discount_rate": {
            "type": "ratio",
            "required": "do_valuation",
            "about": gettext(
                "The annual market discount rate in the price of carbon, "
                "which reflects society's preference for immediate benefits "
                "over future benefits. Required if Run Valuation model is "
                "selected."),
            "name": gettext("annual market discount rate")
        },
        "rate_change": {
            "type": "ratio",
            "required": "do_valuation",
            "about": gettext(
                "The relative annual increase of the price of carbon. "
                "Required if Run Valuation model is selected."),
            "name": gettext("annual price change")
        }
    },
    "outputs": {
        "report.html": {
            "about": "This file presents a summary of all data computed by the model. It also includes descriptions of all other output files produced by the model, so it is a good place to begin exploring and understanding model results. Because this is an HTML file, it can be opened with any web browser."
        },
        "tot_c_cur.tif": {
            "about": "Raster showing the amount of carbon stored in each pixel for the current scenario. It is a sum of all of the carbon pools provided by the biophysical table.",
            "bands": {1: {
                "type": "number",
                "units": u.metric_ton/u.pixel
            }}
        },
        "tot_c_fut.tif": {
            "about": "Raster showing the amount of carbon stored in each pixel for the future scenario. It is a sum of all of the carbon pools provided by the biophysical table.",
            "bands": {1: {
                "type": "number",
                "units": u.metric_ton/u.pixel
            }},
            "created_if": "lulc_fut_path"
        },
        "tot_c_redd.tif": {
            "about": "Raster showing the amount of carbon stored in each pixel for the REDD scenario. It is a sum of all of the carbon pools provided by the biophysical table.",
            "bands": {1: {
                "type": "number",
                "units": u.metric_ton/u.pixel
            }},
            "created_if": "lulc_redd_path"
        },
        "delta_cur_fut.tif": {
            "about": "Raster showing the difference in carbon stored between the future landscape and the current landscape. In this map some values may be negative and some positive. Positive values indicate sequestered carbon, negative values indicate carbon that was lost.",
            "bands": {1: {
                "type": "number",
                "units": u.metric_ton/u.pixel
            }},
            "created_if": "lulc_fut_path"
        },
        "delta_cur_redd.tif": {
            "about": "Raster showing the difference in carbon stored between the REDD landscape and the current landscape. In this map some values may be negative and some positive. Positive values indicate sequestered carbon, negative values indicate carbon that was lost.",
            "bands": {1: {
                "type": "number",
                "units": u.metric_ton/u.pixel
            }},
            "created_if": "lulc_redd_path"
        },
        "npv_fut.tif": {
            "about": "Rasters showing the economic value of carbon sequestered between the current and the future landscape dates.",
            "bands": {1: {
                "type": "number",
                "units": u.currency/u.pixel
            }},
            "created_if": "lulc_fut_path"
        },
        "npv_redd.tif": {
            "about": "Rasters showing the economic value of carbon sequestered between the current and the REDD landscape dates.",
            "bands": {1: {
                "type": "number",
                "units": u.currency/u.pixel
            }},
            "created_if": "lulc_redd_path"
        },
        "intermediate_outputs": {
            "type": "directory",
            "contents": {
                **CARBON_OUTPUTS
            }
        },
        "taskgraph_cache": spec_utils.TASKGRAPH_DIR
    }
}

_OUTPUT_BASE_FILES = {
    'tot_c_cur': 'tot_c_cur.tif',
    'tot_c_fut': 'tot_c_fut.tif',
    'tot_c_redd': 'tot_c_redd.tif',
    'delta_cur_fut': 'delta_cur_fut.tif',
    'delta_cur_redd': 'delta_cur_redd.tif',
    'npv_fut': 'npv_fut.tif',
    'npv_redd': 'npv_redd.tif',
    'html_report': 'report.html',
}

_INTERMEDIATE_BASE_FILES = {
    'c_above_cur': 'c_above_cur.tif',
    'c_below_cur': 'c_below_cur.tif',
    'c_soil_cur': 'c_soil_cur.tif',
    'c_dead_cur': 'c_dead_cur.tif',
    'c_deepsoil': 'c_deepsoil_cur.tif',
    'c_above_fut': 'c_above_fut.tif',
    'c_below_fut': 'c_below_fut.tif',
    'c_soil_fut': 'c_soil_fut.tif',
    'c_dead_fut': 'c_dead_fut.tif',
    'c_deepsoil': 'c_deepsoil_fut.tif',
    'c_above_redd': 'c_above_redd.tif',
    'c_below_redd': 'c_below_redd.tif',
    'c_soil_redd': 'c_soil_redd.tif',
    'c_dead_redd': 'c_dead_redd.tif',
    'c_deepsoil': 'c_deepsoil_redd.tif',
}

_TMP_BASE_FILES = {
    'aligned_lulc_cur_path': 'aligned_lulc_cur.tif',
    'aligned_lulc_fut_path': 'aligned_lulc_fut.tif',
    'aligned_lulc_redd_path': 'aligned_lulc_redd.tif',
}

# -1.0 since carbon stocks are 0 or greater
_CARBON_NODATA = -1.0

def execute(args):
    """Carbon.

    Calculate the amount of carbon stocks given a landscape, or the difference
    due to a future change, and/or the tradeoffs between that and a REDD
    scenario, and calculate economic valuation on those scenarios.

    The model can operate on a single scenario, a combined present and future
    scenario, as well as an additional REDD scenario.

    Args:
        args['workspace_dir'] (string): a path to the directory that will
            write output and other temporary files during calculation.
        args['results_suffix'] (string): appended to any output file name.
        args['lulc_cur_path'] (string): a path to a raster representing the
            current carbon stocks.
        args['calc_sequestration'] (bool): if true, sequestration should
            be calculated and 'lulc_fut_path' and 'do_redd' should be defined.
        args['lulc_fut_path'] (string): a path to a raster representing future
            landcover scenario.  Optional, but if present and well defined
            will trigger a sequestration calculation.
        args['do_redd'] ( bool): if true, REDD analysis should be calculated
            and 'lulc_redd_path' should be defined
        args['lulc_redd_path'] (string): a path to a raster representing the
            alternative REDD scenario which is only possible if the
            args['lulc_fut_path'] is present and well defined.
        args['carbon_pools_path'] (string): path to CSV or that indexes carbon
            storage density to lulc codes. (required if 'do_uncertainty' is
            false)
        args['lulc_cur_year'] (int/string): an integer representing the year
            of `args['lulc_cur_path']` used if `args['do_valuation']`
            is True.
        args['lulc_fut_year'](int/string): an integer representing the year
            of `args['lulc_fut_path']` used in valuation if it exists.
            Required if  `args['do_valuation']` is True and
            `args['lulc_fut_path']` is present and well defined.
        args['do_valuation'] (bool): if true then run the valuation model on
            available outputs. Calculate NPV for a future scenario or a REDD
            scenario and report in final HTML document.
        args['price_per_metric_ton_of_c'] (float): Is the present value of
            carbon per metric ton. Used if `args['do_valuation']` is present
            and True.
        args['discount_rate'] (float): Discount rate used if NPV calculations
            are required.  Used if `args['do_valuation']` is  present and
            True.
        args['rate_change'] (float): Annual rate of change in price of carbon
            as a percentage.  Used if `args['do_valuation']` is  present and
            True.
        args['n_workers'] (int): (optional) The number of worker processes to
            use for processing this model.  If omitted, computation will take
            place in the current process.

    Returns:
        None.
    """

    # === [NEW] Time-Series Mode Execution ===
    if args.get("use_time_series", False):
        LOGGER.info("Running in time-series mode.")
        from datetime import datetime
        import csv

        # Parse user-defined time series
        lulc_series = args["lulc_time_series"]  # Expected: list of (path, year)
        pixel_area = abs(pygeoprocessing.get_raster_info(lulc_series[0][0])['pixel_size'][0] *
                         pygeoprocessing.get_raster_info(lulc_series[0][0])['pixel_size'][1])
        carbon_df = validation.get_validated_dataframe(
            args["carbon_pools_path"], **MODEL_SPEC["args"]["carbon_pools_path"])
        summed_carbon = carbon_df[['c_above', 'c_below', 'c_soil', 'c_dead', 'c_deepsoil']].sum(axis=1)
        lulc_to_carbon = dict(zip(carbon_df.index.astype(int), summed_carbon))
        results = process_lulc_time_series(
            lulc_series, lulc_to_carbon, pixel_area, climate_path=CLIMATE_IMPACT_PATH)

        # Output results
        csv_out_path = os.path.join(args["workspace_dir"], f"carbon_time_series_{datetime.now().strftime('%Y%m%d_%H%M')}.csv")
        with open(csv_out_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Year", "Total_Carbon_Stock_MgC"])
            writer.writerows(results)
        LOGGER.info(f"Time-series carbon results written to: {csv_out_path}")
        return
    # === [END NEW] ===

    file_suffix = utils.make_suffix_string(args, 'results_suffix')
    intermediate_output_dir = os.path.join(
        args['workspace_dir'], 'intermediate_outputs')
    output_dir = args['workspace_dir']
    utils.make_directories([intermediate_output_dir, output_dir])

    LOGGER.info('Building file registry')
    file_registry = utils.build_file_registry(
        [(_OUTPUT_BASE_FILES, output_dir),
         (_INTERMEDIATE_BASE_FILES, intermediate_output_dir),
         (_TMP_BASE_FILES, output_dir)], file_suffix)

    carbon_pool_df = validation.get_validated_dataframe(
        args['carbon_pools_path'], **MODEL_SPEC['args']['carbon_pools_path'])

    try:
        n_workers = int(args['n_workers'])
    except (KeyError, ValueError, TypeError):
        # KeyError when n_workers is not present in args
        # ValueError when n_workers is an empty string.
        # TypeError when n_workers is None.
        n_workers = -1  # Synchronous mode.
    graph = taskgraph.TaskGraph(
        os.path.join(args['workspace_dir'], 'taskgraph_cache'), n_workers)

    cell_size_set = set()
    raster_size_set = set()
    valid_lulc_keys = []
    valid_scenarios = []
    tifs_to_summarize = set()  # passed to _generate_report()

    for scenario_type in ['cur', 'fut', 'redd']:
        lulc_key = "lulc_%s_path" % (scenario_type)
        if lulc_key in args and args[lulc_key]:
            raster_info = pygeoprocessing.get_raster_info(args[lulc_key])
            cell_size_set.add(raster_info['pixel_size'])
            raster_size_set.add(raster_info['raster_size'])
            valid_lulc_keys.append(lulc_key)
            valid_scenarios.append(scenario_type)
    if len(cell_size_set) > 1:
        raise ValueError(
            "the pixel sizes of %s are not equivalent. Here are the "
            "different sets that were found in processing: %s" % (
                valid_lulc_keys, cell_size_set))
    if len(raster_size_set) > 1:
        raise ValueError(
            "the raster dimensions of %s are not equivalent. Here are the "
            "different sizes that were found in processing: %s" % (
                valid_lulc_keys, raster_size_set))

    # calculate total carbon storage
    LOGGER.info('Map all carbon pools to carbon storage rasters.')
    carbon_map_task_lookup = {}
    sum_rasters_task_lookup = {}
    for scenario_type in valid_scenarios:
        carbon_map_task_lookup[scenario_type] = []
        storage_path_list = []
        for pool_type in ['c_above', 'c_below', 'c_soil', 'c_dead', 'c_deepsoil']:
            carbon_pool_by_type = carbon_pool_df[pool_type].to_dict()

            lulc_key = 'lulc_%s_path' % scenario_type
            storage_key = '%s_%s' % (pool_type, scenario_type)
            LOGGER.info(
                "Mapping carbon from '%s' to '%s' scenario.",
                lulc_key, storage_key)

            carbon_map_task = graph.add_task(
                _generate_carbon_map,
                args=(args[lulc_key], carbon_pool_by_type,
                      file_registry[storage_key]),
                target_path_list=[file_registry[storage_key]],
                task_name='carbon_map_%s' % storage_key)
            storage_path_list.append(file_registry[storage_key])
            carbon_map_task_lookup[scenario_type].append(carbon_map_task)

        output_key = 'tot_c_' + scenario_type
        LOGGER.info(
            "Calculate carbon storage for '%s'", output_key)

        sum_rasters_task = graph.add_task(
            func=pygeoprocessing.raster_map,
            kwargs=dict(
                op=sum_op,
                rasters=storage_path_list,
                target_path=file_registry[output_key],
                target_nodata=_CARBON_NODATA),
            target_path_list=[file_registry[output_key]],
            dependent_task_list=carbon_map_task_lookup[scenario_type],
            task_name='sum_rasters_for_total_c_%s' % output_key)
        sum_rasters_task_lookup[scenario_type] = sum_rasters_task
        tifs_to_summarize.add(file_registry[output_key])

    # calculate sequestration
    diff_rasters_task_lookup = {}
    for scenario_type in ['fut', 'redd']:
        if scenario_type not in valid_scenarios:
            continue
        output_key = 'delta_cur_' + scenario_type
        LOGGER.info("Calculate sequestration scenario '%s'", output_key)

        diff_rasters_task = graph.add_task(
            func=pygeoprocessing.raster_map,
            kwargs=dict(
                op=numpy.subtract,  # delta = scenario C - current C
                rasters=[file_registry['tot_c_' + scenario_type],
                         file_registry['tot_c_cur']],
                target_path=file_registry[output_key],
                target_nodata=_CARBON_NODATA),
            target_path_list=[file_registry[output_key]],
            dependent_task_list=[
                sum_rasters_task_lookup['cur'],
                sum_rasters_task_lookup[scenario_type]],
            task_name='diff_rasters_for_%s' % output_key)
        diff_rasters_task_lookup[scenario_type] = diff_rasters_task
        tifs_to_summarize.add(file_registry[output_key])

    # calculate net present value
    calculate_npv_tasks = []
    if 'do_valuation' in args and args['do_valuation']:
        LOGGER.info('Constructing valuation formula.')
        valuation_constant = _calculate_valuation_constant(
            int(args['lulc_cur_year']), int(args['lulc_fut_year']),
            float(args['discount_rate']), float(args['rate_change']),
            float(args['price_per_metric_ton_of_c']))

        for scenario_type in ['fut', 'redd']:
            if scenario_type not in valid_scenarios:
                continue
            output_key = 'npv_%s' % scenario_type
            LOGGER.info("Calculating NPV for scenario '%s'", output_key)

            calculate_npv_task = graph.add_task(
                _calculate_npv,
                args=(file_registry['delta_cur_%s' % scenario_type],
                      valuation_constant, file_registry[output_key]),
                target_path_list=[file_registry[output_key]],
                dependent_task_list=[diff_rasters_task_lookup[scenario_type]],
                task_name='calculate_%s' % output_key)
            calculate_npv_tasks.append(calculate_npv_task)
            tifs_to_summarize.add(file_registry[output_key])

    # Report aggregate results
    tasks_to_report = (list(sum_rasters_task_lookup.values())
                       + list(diff_rasters_task_lookup.values())
                       + calculate_npv_tasks)
    _ = graph.add_task(
        _generate_report,
        args=(tifs_to_summarize, args, file_registry),
        target_path_list=[file_registry['html_report']],
        dependent_task_list=tasks_to_report,
        task_name='generate_report')
    graph.join()

    for tmp_filename_key in _TMP_BASE_FILES:
        try:
            tmp_filename = file_registry[tmp_filename_key]
            if os.path.exists(tmp_filename):
                os.remove(tmp_filename)
        except OSError as os_error:
            LOGGER.warning(
                "Can't remove temporary file: %s\nOriginal Exception:\n%s",
                file_registry[tmp_filename_key], os_error)


# element-wise sum function to pass to raster_map
def sum_op(*xs): return numpy.sum(xs, axis=0)


def _accumulate_totals(raster_path):
    """Sum all non-nodata pixels in `raster_path` and return result."""
    nodata = pygeoprocessing.get_raster_info(raster_path)['nodata'][0]
    raster_sum = 0.0
    for _, block in pygeoprocessing.iterblocks((raster_path, 1)):
        # The float64 dtype in the sum is needed to reduce numerical error in
        # the sum.  Users calculated the sum with ArcGIS zonal statistics,
        # noticed a difference and wrote to us about it on the forum.
        raster_sum += numpy.sum(
            block[~pygeoprocessing.array_equals_nodata(
                    block, nodata)], dtype=numpy.float64)
    return raster_sum


def _generate_carbon_map(
        lulc_path, carbon_pool_by_type, out_carbon_stock_path):
    """Generate carbon stock raster by mapping LULC values to carbon pools.

    Args:
        lulc_path (string): landcover raster with integer pixels.
        out_carbon_stock_path (string): path to output raster that will have
            pixels with carbon storage values in them with units of Mg*C
        carbon_pool_by_type (dict): a dictionary that maps landcover values
            to carbon storage densities per area (Mg C/Ha).

    Returns:
        None.
    """
    lulc_info = pygeoprocessing.get_raster_info(lulc_path)
    pixel_area = abs(numpy.prod(lulc_info['pixel_size']))
    carbon_stock_by_type = dict([
        (lulcid, stock * pixel_area / 10**4)
        for lulcid, stock in carbon_pool_by_type.items()])

    reclass_error_details = {
        'raster_name': 'LULC', 'column_name': 'lucode',
        'table_name': 'Carbon Pools'}
    utils.reclassify_raster(
        (lulc_path, 1), carbon_stock_by_type, out_carbon_stock_path,
        gdal.GDT_Float32, _CARBON_NODATA, reclass_error_details)


def _calculate_valuation_constant(
        lulc_cur_year, lulc_fut_year, discount_rate, rate_change,
        price_per_metric_ton_of_c):
    """Calculate a net present valuation constant to multiply carbon storage.

    Args:
        lulc_cur_year (int): calendar year in present
        lulc_fut_year (int): calendar year in future
        discount_rate (float): annual discount rate as a percentage
        rate_change (float): annual change in price of carbon as a percentage
        price_per_metric_ton_of_c (float): currency amount of Mg of carbon

    Returns:
        a floating point number that can be used to multiply a delta carbon
        storage value by to calculate NPV.
    """
    n_years = lulc_fut_year - lulc_cur_year
    ratio = (
        1 / ((1 + discount_rate / 100) *
             (1 + rate_change / 100)))
    valuation_constant = (price_per_metric_ton_of_c / n_years)
    # note: the valuation formula in the user's guide uses sum notation.
    # here it's been simplified to remove the sum using the general rule
    # sum(r^k) from k=0 to N  ==  (r^(N+1) - 1) / (r - 1)
    # where N = n_years-1 and r = ratio
    if ratio == 1:
        # if ratio == 1, we would divide by zero in the equation below
        # so use the limit as ratio goes to 1, which is n_years
        valuation_constant *= n_years
    else:
        valuation_constant *= (1 - ratio ** n_years) / (1 - ratio)
    return valuation_constant


def _calculate_npv(delta_carbon_path, valuation_constant, npv_out_path):
    """Calculate net present value.

    Args:
        delta_carbon_path (string): path to change in carbon storage over
            time.
        valuation_constant (float): value to multiply each carbon storage
            value by to calculate NPV.
        npv_out_path (string): path to output net present value raster.

    Returns:
        None.
    """
    pygeoprocessing.raster_map(
        op=lambda carbon: carbon * valuation_constant,
        rasters=[delta_carbon_path],
        target_path=npv_out_path)


def _generate_report(raster_file_set, model_args, file_registry):
    """Generate a human readable HTML report of summary stats of model run.

    Args:
        raster_file_set (set): paths to rasters that need summary stats.
        model_args (dict): InVEST argument dictionary.
        file_registry (dict): file path dictionary for InVEST workspace.

    Returns:
        None.
    """
    html_report_path = file_registry['html_report']
    with codecs.open(html_report_path, 'w', encoding='utf-8') as report_doc:
        # Boilerplate header that defines style and intro header.
        header = (
            """
            <!DOCTYPE html>
            <html lang="en">
            <head>
            <meta charset="utf-8">
            <title>Carbon Results</title>
            <style type="text/css">
                body {
                    --invest-green: #148f68;
                    background: #ffffff;
                    color: #000000;
                    font-family: Roboto, "Helvetica Neue", Arial, sans-serif;
                }
                h1, h2, th {
                    font-weight: bold;
                }
                h1, h2 {
                    color: var(--invest-green);
                }
                h1 {
                    font-size: 2rem;
                }
                h2 {
                    font-size: 1.5rem;
                }
                table {
                    border: 0.25rem solid var(--invest-green);
                    border-collapse: collapse;
                }
                thead tr {
                    background: #e9ecef;
                    border-bottom: 0.1875rem solid var(--invest-green);
                }
                tbody tr:nth-child(even) {
                    background: ghostwhite;
                }
                th {
                    padding: 0.5rem;
                    text-align:left;
                }
                td {
                    padding: 0.375rem 0.5rem;
                }
                .number {
                    text-align: right;
                    font-family: monospace;
                }
            </style>
            </head>
            <body>
            <h1>InVEST Carbon Model Results</h1>
            <p>This document summarizes the results from
            running the InVEST carbon model with the following data.</p>
            """
        )

        report_doc.write(header)
        report_doc.write('<p>Report generated at %s</p>' % (
            time.strftime("%Y-%m-%d %H:%M")))

        # Report input arguments
        report_doc.write('<h2>Inputs</h2>')
        report_doc.write('<table><thead><tr><th>arg id</th><th>arg value</th>'
                         '</tr></thead><tbody>')
        for key, value in model_args.items():
            report_doc.write('<tr><td>%s</td><td>%s</td></tr>' % (key, value))
        report_doc.write('</tbody></table>')

        # Report aggregate results
        report_doc.write('<h2>Aggregate Results</h2>')
        report_doc.write(
            '<table><thead><tr><th>Description</th><th>Value</th><th>Units'
            '</th><th>Raw File</th></tr></thead><tbody>')

        # value lists are [sort priority, description, statistic, units]
        report = [
            (file_registry['tot_c_cur'], 'Total cur', 'Mg of C'),
            (file_registry['tot_c_fut'], 'Total fut', 'Mg of C'),
            (file_registry['tot_c_redd'], 'Total redd', 'Mg of C'),
            (file_registry['delta_cur_fut'], 'Change in C for fut', 'Mg of C'),
            (file_registry['delta_cur_redd'],
             'Change in C for redd', 'Mg of C'),
            (file_registry['npv_fut'],
             'Net present value from cur to fut', 'currency units'),
            (file_registry['npv_redd'],
             'Net present value from cur to redd', 'currency units'),
        ]

        for raster_uri, description, units in report:
            if raster_uri in raster_file_set:
                summary_stat = _accumulate_totals(raster_uri)
                report_doc.write(
                    '<tr><td>%s</td><td class="number">%.2f</td><td>%s</td>'
                    '<td>%s</td></tr>' % (
                        description, summary_stat, units, raster_uri))
        report_doc.write('</tbody></table></body></html>')


@validation.invest_validator
def validate(args, limit_to=None):
    """Validate args to ensure they conform to `execute`'s contract.

    Args:
        args (dict): dictionary of key(str)/value pairs where keys and
            values are specified in `execute` docstring.
        limit_to (str): (optional) if not None indicates that validation
            should only occur on the args[limit_to] value. The intent that
            individual key validation could be significantly less expensive
            than validating the entire `args` dictionary.

    Returns:
        list of ([invalid key_a, invalid_keyb, ...], 'warning/error message')
            tuples. Where an entry indicates that the invalid keys caused
            the error message in the second part of the tuple. This should
            be an empty list if validation succeeds.
    """
    return validation.validate(
        args, MODEL_SPEC['args'], MODEL_SPEC['args_with_spatial_overlap'])
