#!/usr/bin/env python
import codecs
import filecmp
import hashlib
import multiprocessing
import os
import shutil
from unittest import mock

import jinja2.exceptions
import pytest
import tarfile
import stat
import pandas as pd
import requests
from pyspark.sql import SparkSession
from requests import Response

import mlflow
from mlflow.exceptions import MissingConfigException
from mlflow.protos.databricks_artifacts_pb2 import ArtifactCredentialType
from mlflow.utils import file_utils
from mlflow.utils.file_utils import (
    get_parent_dir,
    _copy_file_or_tree,
    read_parquet_as_pandas_df,
    write_pandas_df_as_parquet,
    write_spark_dataframe_to_parquet_on_local_disk,
    TempDir,
    _handle_readonly_on_windows,
    local_file_uri_to_path,
    parallelized_download_file_using_http_uri,
)
from mlflow.utils.os import is_windows
from tests.projects.utils import TEST_PROJECT_DIR

from tests.helper_functions import random_int, random_file, safe_edit_yaml


@pytest.fixture(scope="module")
def spark_session():
    session = SparkSession.builder.master("local[*]").getOrCreate()
    yield session
    session.stop()


def test_yaml_read_and_write(tmpdir):
    temp_dir = str(tmpdir)
    yaml_file = random_file("yaml")
    long_value = 1  # pylint: disable=undefined-variable
    data = {
        "a": random_int(),
        "B": random_int(),
        "text_value": "中文",
        "long_value": long_value,
        "int_value": 32,
        "text_value_2": "hi",
    }
    file_utils.write_yaml(temp_dir, yaml_file, data)
    read_data = file_utils.read_yaml(temp_dir, yaml_file)
    assert data == read_data
    yaml_path = os.path.join(temp_dir, yaml_file)
    with codecs.open(yaml_path, encoding="utf-8") as handle:
        contents = handle.read()
    assert "!!python" not in contents
    # Check that UTF-8 strings are written properly to the file (rather than as ASCII
    # representations of their byte sequences).
    assert "中文" in contents

    def edit_func(old_dict):
        old_dict["more_text"] = "西班牙语"
        return old_dict

    assert "more_text" not in file_utils.read_yaml(temp_dir, yaml_file)
    with safe_edit_yaml(temp_dir, yaml_file, edit_func):
        editted_dict = file_utils.read_yaml(temp_dir, yaml_file)
        assert "more_text" in editted_dict
        assert editted_dict["more_text"] == "西班牙语"
    assert "more_text" not in file_utils.read_yaml(temp_dir, yaml_file)


def test_render_and_merge_yaml(tmpdir):
    import json

    json_file = random_file("json")
    extra_config = {"key": 123}
    with open(tmpdir / json_file, "w") as f:
        json.dump(extra_config, f)

    template_yaml_file = random_file("yaml")
    with open(tmpdir / template_yaml_file, "w") as f:
        f.write(
            """
            steps:
              preprocess:
                train_ratio: {{ MY_TRAIN_RATIO|default(0.5) }}
                experiment:
                  tracking_uri: {{ MY_MLFLOW_SERVER|default("https://localhost:5000") }}
            test_1: [1, 2, 3]
            test_2: {{ TEST_VAR_1 }}
            test_3: {{ TEST_VAR_2 }}
            test_4: {{ TEST_VAR_4 }}
            """
            + rf"test_5: {{{{ ('{json_file}' | from_json)['key'] }}}}"
        )
    context_yaml_file = random_file("yaml")
    with open(tmpdir / context_yaml_file, "w") as f:
        f.write(
            """
            MY_MLFLOW_SERVER: "./mlruns"
            TEST_VAR_1: ["a", 1.2]
            TEST_VAR_2: {"a": 2}
            """
            + rf"TEST_VAR_4: {{{{ ('{json_file}' | from_json)['key'] }}}}"
        )

    with tmpdir.as_cwd():
        result = file_utils.render_and_merge_yaml(tmpdir, template_yaml_file, context_yaml_file)
    expected = {
        "MY_MLFLOW_SERVER": "./mlruns",
        "TEST_VAR_1": ["a", 1.2],
        "TEST_VAR_2": {"a": 2},
        "TEST_VAR_4": 123,
        "steps": {"preprocess": {"train_ratio": 0.5, "experiment": {"tracking_uri": "./mlruns"}}},
        "test_1": [1, 2, 3],
        "test_2": ["a", 1.2],
        "test_3": {"a": 2},
        "test_4": 123,
        "test_5": 123,
    }
    assert result == expected


def test_render_and_merge_yaml_raise_on_duplicate_keys(tmpdir):
    template_yaml_file = random_file("yaml")
    with open(tmpdir / template_yaml_file, "w") as f:
        f.write(
            """
            steps: 1
            steps: 2
            test_2: {{ TEST_VAR_1 }}
            """
        )

    context_yaml_file = random_file("yaml")
    file_utils.write_yaml(str(tmpdir), context_yaml_file, {"TEST_VAR_1": 3})

    with pytest.raises(ValueError, match="Duplicate 'steps' key found"):
        file_utils.render_and_merge_yaml(tmpdir, template_yaml_file, context_yaml_file)


def test_render_and_merge_yaml_raise_on_non_existent_yamls(tmpdir):
    template_yaml_file = random_file("yaml")
    with open(tmpdir / template_yaml_file, "w") as f:
        f.write("""test_1: {{ TEST_VAR_1 }}""")

    context_yaml_file = random_file("yaml")
    file_utils.write_yaml(str(tmpdir), context_yaml_file, {"TEST_VAR_1": 3})

    with pytest.raises(MissingConfigException, match="does not exist"):
        file_utils.render_and_merge_yaml(tmpdir, "invalid_name", context_yaml_file)
    with pytest.raises(MissingConfigException, match="does not exist"):
        file_utils.render_and_merge_yaml("invalid_path", template_yaml_file, context_yaml_file)
    with pytest.raises(MissingConfigException, match="does not exist"):
        file_utils.render_and_merge_yaml(tmpdir, template_yaml_file, "invalid_name")


def test_render_and_merge_yaml_raise_on_not_found_key(tmpdir):
    template_yaml_file = random_file("yaml")
    with open(tmpdir / template_yaml_file, "w") as f:
        f.write("""test_1: {{ TEST_VAR_1 }}""")

    context_yaml_file = random_file("yaml")
    file_utils.write_yaml(str(tmpdir), context_yaml_file, {})

    with pytest.raises(jinja2.exceptions.UndefinedError, match="'TEST_VAR_1' is undefined"):
        file_utils.render_and_merge_yaml(tmpdir, template_yaml_file, context_yaml_file)


def test_yaml_write_sorting(tmpdir):
    temp_dir = str(tmpdir)
    data = {
        "a": 1,
        "c": 2,
        "b": 3,
    }

    sorted_yaml_file = random_file("yaml")
    file_utils.write_yaml(temp_dir, sorted_yaml_file, data, sort_keys=True)
    expected_sorted = """a: 1
b: 3
c: 2
"""
    with open(os.path.join(temp_dir, sorted_yaml_file)) as f:
        actual_sorted = f.read()

    assert actual_sorted == expected_sorted

    unsorted_yaml_file = random_file("yaml")
    file_utils.write_yaml(temp_dir, unsorted_yaml_file, data, sort_keys=False)
    expected_unsorted = """a: 1
c: 2
b: 3
"""
    with open(os.path.join(temp_dir, unsorted_yaml_file)) as f:
        actual_unsorted = f.read()

    assert actual_unsorted == expected_unsorted


def test_mkdir(tmpdir):
    temp_dir = str(tmpdir)
    new_dir_name = "mkdir_test_%d" % random_int()
    file_utils.mkdir(temp_dir, new_dir_name)
    assert os.listdir(temp_dir) == [new_dir_name]

    with pytest.raises(OSError, match="bad directory"):
        file_utils.mkdir("/   bad directory @ name ", "ouch")

    # does not raise if directory exists already
    file_utils.mkdir(temp_dir, new_dir_name)

    # raises if it exists already but is a file
    dummy_file_path = str(tmpdir.join("dummy_file"))
    open(dummy_file_path, "a").close()
    with pytest.raises(OSError, match="exists"):
        file_utils.mkdir(dummy_file_path)


def test_make_tarfile(tmpdir):
    # Tar a local project
    tarfile0 = str(tmpdir.join("first-tarfile"))
    file_utils.make_tarfile(
        output_filename=tarfile0, source_dir=TEST_PROJECT_DIR, archive_name="some-archive"
    )
    # Copy local project into a temp dir
    dst_dir = str(tmpdir.join("project-directory"))
    shutil.copytree(TEST_PROJECT_DIR, dst_dir)
    # Tar the copied project
    tarfile1 = str(tmpdir.join("second-tarfile"))
    file_utils.make_tarfile(
        output_filename=tarfile1, source_dir=dst_dir, archive_name="some-archive"
    )
    # Compare the archives & explicitly verify their SHA256 hashes match (i.e. that
    # changes in file modification timestamps don't affect the archive contents)
    assert filecmp.cmp(tarfile0, tarfile1, shallow=False)
    with open(tarfile0, "rb") as first_tar, open(tarfile1, "rb") as second_tar:
        assert (
            hashlib.sha256(first_tar.read()).hexdigest()
            == hashlib.sha256(second_tar.read()).hexdigest()
        )
    # Extract the TAR and check that its contents match the original directory
    extract_dir = str(tmpdir.join("extracted-tar"))
    os.makedirs(extract_dir)
    with tarfile.open(tarfile0, "r:gz") as handle:
        handle.extractall(path=extract_dir)
    dir_comparison = filecmp.dircmp(os.path.join(extract_dir, "some-archive"), TEST_PROJECT_DIR)
    assert len(dir_comparison.left_only) == 0
    assert len(dir_comparison.right_only) == 0
    assert len(dir_comparison.diff_files) == 0
    assert len(dir_comparison.funny_files) == 0


def test_get_parent_dir(tmpdir):
    child_dir = tmpdir.join("dir").mkdir()
    assert str(tmpdir) == get_parent_dir(str(child_dir))


def test_file_copy():
    with TempDir() as tmp:
        file_path = tmp.path("test_file.txt")
        copy_path = tmp.path("test_dir1/")
        os.mkdir(copy_path)
        with open(file_path, "a") as f:
            f.write("testing")
        _copy_file_or_tree(file_path, copy_path, "")
        assert filecmp.cmp(file_path, os.path.join(copy_path, "test_file.txt"))


def test_dir_create():
    with TempDir() as tmp:
        file_path = tmp.path("test_file.txt")
        create_dir = tmp.path("test_dir2/")
        with open(file_path, "a") as f:
            f.write("testing")
        name = _copy_file_or_tree(file_path, file_path, create_dir)
        assert filecmp.cmp(file_path, name)


def test_dir_copy():
    with TempDir() as tmp:
        dir_path = tmp.path("test_dir1/")
        copy_path = tmp.path("test_dir2")
        os.mkdir(dir_path)
        with open(os.path.join(dir_path, "test_file.txt"), "a") as f:
            f.write("testing")
        _copy_file_or_tree(dir_path, copy_path, "")
        assert filecmp.dircmp(dir_path, copy_path)


def test_read_and_write_parquet():
    file_source = "sample-file-to-write"
    data_frame = pd.DataFrame({"horizon": 10, "frequency": "W"}, index=[0])
    write_pandas_df_as_parquet(data_frame, file_source)
    serialized_data_frame = read_parquet_as_pandas_df(file_source)
    pd.testing.assert_frame_equal(data_frame, serialized_data_frame)


def test_write_spark_df_to_parquet(spark_session, tmp_path):
    sdf = spark_session.createDataFrame(
        [
            (0, "a b c d e spark", 1.0),
            (1, "b d", 0.0),
            (2, "spark f g h", 1.0),
            (3, "hadoop mapreduce", 0.0),
        ],
        ["id", "text", "label"],
    )
    output_path = str(tmp_path / "output")
    write_spark_dataframe_to_parquet_on_local_disk(sdf, output_path)
    pd.testing.assert_frame_equal(sdf.toPandas(), pd.read_parquet(output_path))


@pytest.mark.skipif(not is_windows(), reason="requires Windows")
def test_handle_readonly_on_windows(tmpdir):
    tmp_path = tmpdir.join("file").strpath
    with open(tmp_path, "w"):
        pass

    # Make the file read-only
    os.chmod(tmp_path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
    # Ensure the file can't be removed
    with pytest.raises(PermissionError, match="Access is denied") as exc:
        os.unlink(tmp_path)

    _handle_readonly_on_windows(
        os.unlink,
        tmp_path,
        (exc.type, exc.value, exc.traceback),
    )
    assert not os.path.exists(tmp_path)


@pytest.mark.skipif(not is_windows(), reason="This test only passes on Windows")
@pytest.mark.parametrize(
    ("input_uri", "expected_path"),
    [
        ("file://my_server/my_path/my_sub_path", r"\\my_server\my_path\my_sub_path"),
        (r"\\my_server\my_path\my_sub_path", r"\\my_server\my_path\my_sub_path"),
    ],
)
def test_local_file_uri_to_path_on_windows(input_uri, expected_path):
    assert local_file_uri_to_path(input_uri) == expected_path


def test_shutil_copytree_without_file_permissions(tmp_path):
    src_dir = tmp_path.joinpath("src-dir")
    src_dir.mkdir()
    dst_dir = tmp_path.joinpath("dst-dir")
    dst_dir.mkdir()
    # Test copying empty directory
    mlflow.utils.file_utils.shutil_copytree_without_file_permissions(src_dir, dst_dir)
    assert len(os.listdir(dst_dir)) == 0
    # Test copying directory with contents
    src_dir.joinpath("subdir").mkdir()
    src_dir.joinpath("subdir/subdir-file.txt").write_text("testing 123")
    src_dir.joinpath("top-level-file.txt").write_text("hi")
    mlflow.utils.file_utils.shutil_copytree_without_file_permissions(src_dir, dst_dir)
    assert set(os.listdir(dst_dir)) == {"top-level-file.txt", "subdir"}
    assert set(os.listdir(dst_dir.joinpath("subdir"))) == {"subdir-file.txt"}
    assert dst_dir.joinpath("subdir/subdir-file.txt").read_text() == "testing 123"
    assert dst_dir.joinpath("top-level-file.txt").read_text() == "hi"


@pytest.mark.skipif(is_windows(), reason="This test fails on Windows")
def test_parallelized_download_file_using_http_uri_requests_appropriate_chunks(tmp_path):
    calls_kwargs = multiprocessing.Manager().list([])

    # Get call kwargs manually. Calls are not properly stored in a MagicMock object
    # in a multiprocessing context.
    def mock_request_side_effect(method, url, *args, **kwargs):
        calls_kwargs.append(kwargs)
        response_mock = Response()
        response_mock.status_code = 206
        response_mock._content = b"\x01\x01"
        return response_mock

    with mock.patch("requests.Session.request", side_effect=mock_request_side_effect):
        parallelized_download_file_using_http_uri(
            "fake_uri",
            tmp_path / "testfile",
            file_size=1000,
            uri_type=ArtifactCredentialType.AWS_PRESIGNED_URL,
            chunk_size=100,
            headers={},
        )
    requested_ranges = [call_kwargs["headers"]["Range"] for call_kwargs in calls_kwargs]
    assert len(requested_ranges) == 10
    expected_ranges = [f"bytes={100*i}-{(100*(i+1))-1}" for i in range(10)]
    assert sorted(requested_ranges) == expected_ranges


@pytest.mark.skipif(is_windows(), reason="This test fails on Windows")
@pytest.mark.parametrize("uri_type", [ArtifactCredentialType.GCP_SIGNED_URL, None])
def test_parallelized_download_file_using_http_uri_handles_gcp_transcoding(tmp_path, uri_type):
    calls_kwargs = multiprocessing.Manager().list([])
    file_content = b"\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01"

    def mock_request_side_effect(method, url, *args, **kwargs):
        calls_kwargs.append(kwargs)
        response_mock = Response()
        response_mock.status_code = 200
        # 10-byte file
        response_mock._content = file_content
        return response_mock

    filename = tmp_path / "testfile"
    with mock.patch("requests.Session.request") as request_mock:
        request_mock.side_effect = mock_request_side_effect
        failed_downloads = parallelized_download_file_using_http_uri(
            "fake_uri",
            filename,
            file_size=10,
            uri_type=uri_type,
            chunk_size=2,
            headers={},
        )
    # Should only have called once because the whole file was returned
    assert failed_downloads == {}
    assert len(calls_kwargs) == 1
    with open(filename, "rb") as f:
        f.seek(0)
        contents = f.read()
        assert contents == file_content


@pytest.mark.skipif(is_windows(), reason="This test fails on Windows")
def test_parallelized_download_file_using_http_uri_returns_errors_correctly(tmp_path):
    calls_kwargs = multiprocessing.Manager().list([])
    file_content = b"\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01"

    def mock_request_side_effect(method, url, *args, **kwargs):
        calls_kwargs.append(kwargs)
        response_mock = Response()
        response_mock.status_code = 200
        # 10-byte file
        response_mock._content = file_content
        # Randomly fail for the first chunk
        if kwargs["headers"]["Range"].startswith("bytes=0"):
            raise requests.HTTPError("test exception")
        return response_mock

    with mock.patch("requests.Session.request") as request_mock:
        request_mock.side_effect = mock_request_side_effect
        failed_downloads = parallelized_download_file_using_http_uri(
            "fake_uri",
            tmp_path / "testfile",
            file_size=10,
            uri_type=ArtifactCredentialType.AWS_PRESIGNED_URL,
            chunk_size=2,
            headers={},
        )
        assert len(failed_downloads) == 1
        assert str(failed_downloads[0]) == "test exception"
