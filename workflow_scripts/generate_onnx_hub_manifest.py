import hashlib
import json
import os
import re
import bs4
import markdown
import pandas as pd
import typepy
from os.path import join, split
import onnxruntime as ort
from onnxruntime.capi.onnxruntime_pybind11_state import NotImplemented
import onnx
from onnx import shape_inference


# Acknowledgments to pytablereader codebase for this function
def parse_html(table):
    headers = []
    data_matrix = []
    rows = table.find_all("tr")
    re_table_val = re.compile("td|th")
    for row in rows:
        td_list = row.find_all("td")
        if typepy.is_empty_sequence(td_list):
            if typepy.is_not_empty_sequence(headers):
                continue
            th_list = row.find_all("th")
            if typepy.is_empty_sequence(th_list):
                continue
            headers = [row.text.strip() for row in th_list]
            continue
        data_matrix.append(list(row.find_all(re_table_val)))

    if typepy.is_empty_sequence(data_matrix):
        raise ValueError("data matrix is empty")

    return pd.DataFrame(data_matrix, columns=headers)


def parse_readme(filename):
    with open(filename, "r") as f:
        parsed = markdown.markdown(f.read(), extensions=["markdown.extensions.tables"])
        soup = bs4.BeautifulSoup(parsed, "html.parser")
        return [parse_html(table) for table in soup.find_all("table")]


top_level_readme = join("..", "README.md")
top_level_tables = parse_readme(top_level_readme)
markdown_files = set()
for top_level_table in top_level_tables:
    for i, row in top_level_table.iterrows():
        if "Model Class" in row:
            try:
                markdown_files.add(join(
                    "..", row["Model Class"].contents[0].contents[0].attrs['href'], "README.md"))
            except AttributeError:
                print("{} has no link to implementation".format(row["Model Class"].contents[0]))
# Sort for reproducibility
markdown_files = sorted(list(markdown_files))

all_tables = []
for markdown_file in markdown_files:
    with open(markdown_file, "r") as f:
        for parsed in parse_readme(markdown_file):
            parsed = parsed.rename(columns={"Opset Version": "Opset version"})
            if all(col in parsed.columns.values for col in ["Model", "Download", "Opset version", "ONNX version"]):
                parsed["source_file"] = markdown_file
                all_tables.append(parsed)
            else:
                print("Unrecognized table columns in file {}: {}".format(markdown_file, parsed.columns.values))

df = pd.concat(all_tables, axis=0)
normalize_name = {
    "Download": "model_path",
    "Download (with sample test data)": "model_with_data_path",
}

top_level_fields = ["model", "model_path", "opset_version", "onnx_version"]


def prep_name(col):
    if col in normalize_name:
        col = normalize_name[col]
    col = col.rstrip()
    prepped_col = col.replace(" ", "_").lower()
    if prepped_col in top_level_fields:
        return prepped_col
    else:
        return col


renamed = df.rename(columns={col: prep_name(col) for col in df.columns.values})
metadata_fields = [f for f in renamed.columns.values if f not in top_level_fields]


def get_file_info(row, field):
    source_dir = split(row["source_file"])[0]
    model_file = row[field].contents[0].attrs["href"]
    ## So that model relative path is consistent across OS
    rel_path = "/".join(join(source_dir, model_file).split(os.sep)[1:])
    with open(join("..", rel_path), "rb") as f:
        bytes = f.read()
        sha256 = hashlib.sha256(bytes).hexdigest()
    return {
        field: rel_path,
        field.replace("_path", "") + "_sha": sha256,
        field.replace("_path", "") + "_bytes": len(bytes),
    }


def get_model_tags(row):
    source_dir = split(row["source_file"])[0]
    raw_tags = source_dir.split("/")[1:]
    return [tag.replace("_", " ") for tag in raw_tags]


def get_model_ports(source_file, metadata, model_name):
    model_path = join("..", source_file)
    try:
        # Hide graph warnings. Severity 3 means error and above.
        ort.set_default_logger_severity(3)
        # Start from ORT 1.10, ORT requires explicitly setting the providers parameter
        # if you want to use execution providers
        # other than the default CPU provider (as opposed to the previous behavior of
        # providers getting set/registered by default
        # based on the build flags) when instantiating InferenceSession.
        # For example, if NVIDIA GPU is available and ORT Python package is built with CUDA, then call API as following:
        # ort.InferenceSession(path/to/model, providers=['CUDAExecutionProvider'])
        session = ort.InferenceSession(model_path)
        inputs = session.get_inputs()
        outputs = session.get_outputs()
        io_ports = {
            "inputs": [{"name": input.name, "shape": input.shape, "type": input.type} for input in inputs],
            "outputs": [{"name": output.name, "shape": output.shape, "type": output.type} for output in outputs],
        }

        extra_ports = None
        if "classification" in metadata["tags"]:
            inferred_model = shape_inference.infer_shapes(onnx.load(model_path))
            nodes = list(inferred_model.graph.value_info)
            if model_name in feature_tensor_names:
                node_name = feature_tensor_names[model_name]
                node = [n for n in nodes if n.name == node_name][0]
                shape = [d.dim_value for d in list(node.type.tensor_type.shape.dim)]
                extra_ports = {"features": [
                    {"name": node.name, "shape": shape}
                ]}

        return io_ports, extra_ports

    except NotImplemented:
        print(
            'Failed to load model from {}. Run `git lfs pull --include="{}" --exclude=""` '
            'to download the model payload first.'.format(
                model_path, source_file
            )
        )
        return None, None


feature_tensor_names = {
    'AlexNet': 'fc7_1',
    'CaffeNet': 'fc7_1',
    'DenseNet-121': 'pool5_1',
    'EfficientNet-Lite4': 'efficientnet-lite4/model/head/AvgPool:0',
    'GoogleNet': 'pool5/7x7_s1_2',
    'Inception-1': 'pool5/7x7_s1_2',
    'Inception-2': 'pool5/7x7_s1_1',
    'MobileNet v2-1.0': '464',
    'R-CNN ILSVRC13': 'fc7_1',
    'ResNet18': 'resnetv15_pool1_fwd',
    'ResNet34': 'resnetv16_pool1_fwd',
    'ResNet50': 'resnetv17_pool1_fwd',
    'ResNet101': 'resnetv18_pool1_fwd',
    'ResNet152': 'resnetv19_pool1_fwd',
    'ResNet50_fp32': 'resnetv17_pool1_fwd',
    'ResNet50_int8': 'flatten_473_quantized',
    'ResNet50-caffe2': 'gpu_0/pool5_1',
    'ResNet18-v2': 'resnetv22_pool1_fwd',
    'ResNet34-v2': 'resnetv23_pool1_fwd',
    'ResNet50-v2': 'resnetv24_pool1_fwd',
    'ResNet101-v2': 'resnetv25_pool1_fwd',
    'ResNet152-v2': 'resnetv27_pool1_fwd',
    'ShuffleNet-v1': 'gpu_0/final_avg_1',
    'ShuffleNet-v2': '611',
    'ShuffleNet-v2-fp32': '611',
    'ShuffleNet-v2-int8': '611',
    'SqueezeNet 1.1': 'squeezenet0_pool3_fwd',
    'SqueezeNet 1.0': 'pool10_1',
    'VGG 16': "flatten_70",
    'VGG 16-bn': "flatten_135",
    'VGG 19': "flatten_82",
    'VGG 19-bn': "flatten_162",
    'VGG 16-int8': "flatten_70_quantized",
    'VGG 19-caffe2': "fc7_3",
    'ZFNet-512': 'gpu_0/fc7_2'
}

output = []
for i, row in renamed.iterrows():
    if len(row["model"].contents) > 0 and len(row["model_path"].contents) > 0:
        model_name = row["model"].contents[0]
        model_info = get_file_info(row, "model_path")
        model_path = model_info.pop("model_path")
        metadata = model_info
        metadata["tags"] = get_model_tags(row)
        io_ports, extra_ports = get_model_ports(model_path, metadata, model_name)
        if io_ports is not None:
            metadata["io_ports"] = io_ports
        if extra_ports is not None:
            metadata["extra_ports"] = extra_ports

        try:
            for k, v in get_file_info(row, "model_with_data_path").items():
                metadata[k] = v
        except (AttributeError, FileNotFoundError) as e:
            print("no model_with_data in file {}".format(row["source_file"]))

        try:
            opset = int(row["opset_version"].contents[0])
        except ValueError:
            print("malformed opset {} in {}".format(row["opset_version"].contents[0], row["source_file"]))
            continue

        output.append(
            {
                "model": model_name,
                "model_path": model_path,
                "onnx_version": row["onnx_version"].contents[0],
                "opset_version": int(row["opset_version"].contents[0]),
                "metadata": metadata
            }
        )
    else:
        print("Missing model in {}".format(row["source_file"]))

with open(join("..", "ONNX_HUB_MANIFEST.json"), "w+") as f:
    print("Found {} models".format(len(output)))
    json.dump(output, f, indent=4)
