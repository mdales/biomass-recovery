import logging
import re
from typing import Any, List, Union

import geopandas as gpd
import pandas as pd
import pyproj
import sqlalchemy as db
from sqlalchemy import create_engine, inspect

from biomassrecovery.constants import DB_CONFIG, WGS84
from biomassrecovery.utils.logging_util import get_logger

import warnings

logger = get_logger(__file__)

COLUMN_CHECK_RE = re.compile("(.+)\((.+)\)")
COLUMN_OPERATORS = ["AVG", "COUNT", "SUM"]

def gedi_sql_query(
    table_name: str,
    columns: str = "*",
    geometry: gpd.GeoSeries = None,
    crs: str = WGS84,
    start_time: str = None,
    end_time: str = None,
    limit: int = None,
    force: bool = False,
    order_by: List[str] = [],
    **filters
):
    conditions = []
    # Temporal conditions
    if start_time is not None and end_time is not None:
        conditions += [f"(absolute_time between '{start_time}' AND '{end_time}')"]
    # Spatial conditions
    if geometry is not None:
        crs = pyproj.CRS.from_user_input(crs)

        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="__len__ for multi-part geometries is deprecated and will be removed in Shapely 2.0",
            )
            queries = [
                "ST_Intersects(geometry, "
                f"ST_GeomFromText('{x}', {crs.to_epsg()}))"
                for x in geometry.to_wkt().values
            ]
            # Parenthesis here are important, as AND has precedence, so if you
            # don't enclose this you'll just restrict the final entry in the list
            conditions += [
                f"({' OR '.join(queries)})"
            ]

    for column in filters:
        value = filters[column]
        comparitor = "="

        def _escape_value(v: Any) -> Any:
            if isinstance(v, str):
                return f"'{v}'"
            return v

        if isinstance(value, list):
            comparitor = "IN"
            value = [_escape_value(x) for x in value]
        else:
            value = _escape_value(value)
        conditions += [
            f"({column} {comparitor} {value})"
        ]

    # Combining conditions
    condition = (
        f" WHERE {' AND '.join(conditions)}" if len(conditions) > 0 else ""
    )
    # Setting limits
    limits = f" LIMIT {limit}" if limit is not None else ""

    if not force and condition == "" and limit is None:
        raise UserWarning(
            "Warning! This will load the entire table. To proceed set `force`=True."
        )

    # Order by clauses
    order = "" if len(order_by) == 0 else " ORDER BY "
    order += ", ".join([(f"{x[1:]} DESC" if x[0] == "-" else x) for x in order_by])

    sql_query = (
        f"SELECT {', '.join(columns)} FROM {table_name}" + condition + limits + order
    )
    return sql_query


class GediDatabase(object):
    """Database connector for the GEDI DB."""

    def __init__(self):
        self.engine = create_engine(DB_CONFIG, echo=False)
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore", "Did not recognize type 'geometry' of column"
            )
            self.inspector = inspect(self.engine)

        self.allowed_cols = {}
        for table_name in self.inspector.get_table_names():
            allowed_cols = {
                col["name"] for col in self.inspector.get_columns(table_name)
            }
            self.allowed_cols[table_name] = allowed_cols

    def query(
        self,
        table_name: str,
        columns: Union[str, List[str]] = "*",
        geometry: gpd.GeoDataFrame = None,
        crs: str = WGS84,
        start_time: str = None,
        end_time: str = None,
        limit: int = None,
        use_geopandas: bool = False,
        force: bool = False,
        order_by: List[str] = [],
        **filters
    ) -> pd.DataFrame:

        if table_name not in self.allowed_cols:
            raise ValueError("Unsupported table {table_name}.")

        sql_column_operator_used = False
        if columns != "*":
            for column in columns:
                match = COLUMN_CHECK_RE.match(column)
                if match:
                    operator, column = match.groups()
                    if operator.upper() not in COLUMN_OPERATORS:
                        raise ValueError(
                            f"`{operator}` not allowed. Must be one of {COLUMN_OPERATORS}"
                        )
                    sql_column_operator_used = True
                if (column not in self.allowed_cols[table_name]) and (column is not "*"):
                    raise ValueError(
                        f"`{column}` not allowed. Must be one of {sorted(list(self.allowed_cols[table_name]))} or `*`"
                    )

        # We use these for equality filters as a simple way to expand the
        # where clause. If this was behind an API we'd also want to watch out
        # SQL injection attempts here!
        for column in filters:
            if column not in self.allowed_cols[table_name]:
                raise ValueError(
                    f"`{column}` not allowed as filter. Must be one of {sorted(list(self.allowed_cols[table_name]))}"
                )

        for column in order_by:
            # we use Django style notation for descending with "-column name"
            if len(column) == 0:
                raise ValueError("Empty column names a not allowed for sorting.")
            if column[0] == '-':
                column = column[1:]
                if column not in self.allowed_cols[table_name]:
                    raise ValueError(
                        f"`{column}` not allowed for sorting. Must be one of {sorted(list(self.allowed_cols[table_name]))}"
                    )

        if use_geopandas or geometry is not None and not sql_column_operator_used:
            if columns != "*" and "geometry" not in columns:
                columns += ["geometry"]

            # Construct sql query
            sql_query = gedi_sql_query(
                table_name,
                columns=columns,
                geometry=geometry,
                crs=crs,
                limit=limit,
                start_time=start_time,
                end_time=end_time,
                force=force,
                order_by=order_by,
                **filters
            )

            logger.debug("SQL Query: %s", sql_query)
            return gpd.read_postgis(
                sql_query, con=self.engine, geom_col="geometry"
            )

        else:
            # Construct sql query
            sql_query = gedi_sql_query(
                table_name,
                columns=columns,
                geometry=geometry,
                crs=crs,
                limit=limit,
                start_time=start_time,
                end_time=end_time,
                force=force,
                order_by=order_by,
                **filters
            )

            logger.debug("SQL Query: %s", sql_query)
            return pd.read_sql(sql_query, con=self.engine)