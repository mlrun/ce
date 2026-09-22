import sys
from pathlib import Path
import ruamel.yaml


def difference(d1, d2, keep=[]):
    if isinstance(d1, dict):
        assert isinstance(d2, dict)
        to_delete = set()
        for k, v in d1.items():
            if isinstance(v, (dict, list)):
                difference(v, d2[k], keep=keep)
                continue
            if k in keep:
                continue
            if k in d2:
                to_delete.add(k)
                difference(v, d2[k], keep=keep)
        for k in to_delete:
            del d1[k]

    elif isinstance(d1, list):
        assert isinstance(d2, list)
        to_delete = set()
        for idx, elem in enumerate(d1):
            if isinstance(elem, (dict, list)):
                difference(elem, d2[idx], keep=keep)
            elif elem in d2:
                to_delete.add(elem)
        for elem in to_delete:
            d1.remove(elem)
    return d1


yaml = ruamel.yaml.YAML()
yaml.indent(sequence=4, offset=2)
file_1 = open('/Users/Gilad_Shapira/PycharmProjects/ce/charts/mlrun-ce/values.yaml').read()
file_2 = open('/Users/Gilad_Shapira/PycharmProjects/ce/charts/mlrun-ce/admin_installation_values.yaml').read()
data1 = yaml.load(file_1)
data2 = yaml.load(file_2)
print(data2)
result = difference(data1, data2)
yaml.dump(result, sys.stdout)