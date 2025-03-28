# Carbon Storage and Sequestration Modeling Tool (Time-Series + Climate Impact)

This project estimates land-based carbon stock and sequestration over time using spatial land cover data, extended carbon pools, and optional climate impact modifiers. It adapts and extends concepts from the InVEST Carbon model to meet advanced integrated assessment needs, including high temporal resolution and climate feedback integration.

---
## Features

- Supports **multi-year time series** of land-use/land-cover (LULC) maps
- Handles **extended carbon pools**, including deep soil carbon
- Integrates **climate impact rasters** to reduce carbon storage where relevant
- Outputs carbon stock totals to timestamped `.csv` files
- Designed for policy analysis, IAM scenarios, and sustainability modeling

---

## Project Structure

- carbon_integrated_timeseries.py         # Main modeling script data/
- lulc_2020.tif                           # Example LULC raster (year 2020) 
- carbon_pools.csv                        # CSV lookup: LULC class
- carbon stock climate_impact.tif         # (Optional) Climate modifier raster outputs
- carbon_time_series_YYYYMMDD.csv         # Time-stamped results

---

## Inputs

| Input File             | Description                                                                        |
|------------------------|------------------------------------------------------------------------------------|
| `lulc_time_series`     | List of `(path, year)` pairs (e.g., `[("lulc_2020.tif", 2020), ...]`) |
| `carbon_pools_path`    | CSV with columns: `lucode`, `c_above`, `c_below`, `c_soil`, `c_dead`, `c_deepsoil` |
| `climate_impact.tif`   | *(Optional)* Raster with percent reduction per pixel (0–100) |

---

## Example `args` for Python execution
```python
args = {
    "use_time_series": True,
    "workspace_dir": "outputs/",
    "carbon_pools_path": "data/carbon_pools.csv",
    "lulc_time_series": [
        ("data/lulc_2020.tif", 2020),
        ("data/lulc_2030.tif", 2030)
    ]
}

