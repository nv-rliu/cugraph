# Copyright (c) 2022-2024, NVIDIA CORPORATION.
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

import gc

import pytest

import cupy
import cudf
import cugraph
import cugraph.dask as dcg
import dask_cudf
from cugraph.datasets import karate, dolphins
from cugraph.testing import utils


# =============================================================================
# Pytest Setup / Teardown - called for each test function
# =============================================================================


def setup_function():
    gc.collect()


# =============================================================================
# Parameters
# =============================================================================

DATASETS = [karate, dolphins]
IS_DIRECTED = [True, False]
NORMALIZED = [False, True]
ENDPOINTS = [False, True]
SUBSET_SEED = [42, None]
SUBSET_SIZE = [None, 15]
VERTEX_LIST_TYPE = [list, cudf]

# =============================================================================
# Helper functions
# =============================================================================


def get_sg_graph(dataset, directed):
    G = dataset.get_graph(create_using=cugraph.Graph(directed=directed))

    return G


def get_mg_graph(dataset, directed):
    input_data_path = dataset.get_path()
    blocksize = dcg.get_chunksize(input_data_path)
    ddf = dask_cudf.read_csv(
        input_data_path,
        blocksize=blocksize,
        delimiter=dataset.metadata["delim"],
        names=dataset.metadata["col_names"],
        dtype=dataset.metadata["col_types"],
    )
    dg = cugraph.Graph(directed=directed)
    dg.from_dask_cudf_edgelist(
        ddf,
        source="src",
        destination="dst",
        edge_attr="wgt",
        renumber=True,
        store_transposed=True,
    )

    return dg


@pytest.fixture(scope="module")
def input_expected_output(input_combo):
    """
    This fixture returns the inputs and expected results from the
    betweenness_centrality algo based on cuGraph betweenness_centrality) which can
    be used for validation.
    """

    input_data_path = input_combo["graph_file"]
    normalized = input_combo["normalized"]
    endpoints = input_combo["endpoints"]
    random_state = input_combo["subset_seed"]
    subset_size = input_combo["subset_size"]
    directed = input_combo["directed"]
    vertex_list_type = input_combo["vertex_list_type"]

    G = utils.generate_cugraph_graph_from_file(input_data_path, directed=directed)

    if subset_size is None:
        k = subset_size
    elif isinstance(subset_size, int):
        # Select random vertices
        k = G.select_random_vertices(
            random_state=random_state, num_vertices=subset_size
        )
        if vertex_list_type is list:
            k = k.to_arrow().to_pylist()

        print("the seeds are \n", k)
        if vertex_list_type is int:
            # This internally sample k vertices in betweenness centrality.
            # Since the nodes that will be sampled by each implementation will
            # be random, therefore sample all vertices which will make the test
            # consistent.
            k = len(G.nodes())

    input_combo["k"] = k

    sg_cugraph_bc = cugraph.betweenness_centrality(
        G, k=k, normalized=normalized, endpoints=endpoints, random_state=random_state
    )
    # Save the results back to the input_combo dictionary to prevent redundant
    # cuGraph runs. Other tests using the input_combo fixture will look for
    # them, and if not present they will have to re-run the same cuGraph call.
    sg_cugraph_bc = sg_cugraph_bc.sort_values("vertex").reset_index(drop=True)

    input_combo["sg_cugraph_results"] = sg_cugraph_bc
    chunksize = dcg.get_chunksize(input_data_path)
    ddf = dask_cudf.read_csv(
        input_data_path,
        chunksize=chunksize,
        delimiter=" ",
        names=["src", "dst", "value"],
        dtype=["int32", "int32", "float32"],
    )

    dg = cugraph.Graph(directed=directed)
    dg.from_dask_cudf_edgelist(
        ddf,
        source="src",
        destination="dst",
        edge_attr="value",
        renumber=True,
        store_transposed=True,
    )

    input_combo["MGGraph"] = dg

    return input_combo


# =============================================================================
# Tests
# =============================================================================


# @pytest.mark.skipif(
#    is_single_gpu(), reason="skipping MG testing on Single GPU system"
# )


@pytest.mark.mg
def test_dask_mg_betweenness_centrality(dask_client, benchmark, input_expected_output):

    dg = input_expected_output["MGGraph"]
    k = input_expected_output["k"]
    endpoints = input_expected_output["endpoints"]
    normalized = input_expected_output["normalized"]
    random_state = input_expected_output["subset_seed"]
    mg_bc_results = benchmark(
        dcg.betweenness_centrality,
        dg,
        k=k,
        normalized=normalized,
        endpoints=endpoints,
        random_state=random_state,
    )

    mg_bc_results = (
        mg_bc_results.compute().sort_values("vertex").reset_index(drop=True)
    )["betweenness_centrality"].to_cupy()

    sg_bc_results = (
        input_expected_output["sg_cugraph_results"]
        .sort_values("vertex")
        .reset_index(drop=True)
    )["betweenness_centrality"].to_cupy()

    diff = cupy.isclose(mg_bc_results, sg_bc_results)

    assert diff.all()
