import json


def instantiate(path, **values):
    """讀取 TM/TD 檔，替換 placeholder；若是 Thing Model 則轉為 TD instance"""
    with open(path, encoding="utf-8") as f:
        text = f.read()

    for name, value in values.items():
        text = text.replace("{{" + name + "}}", str(value))

    thing = json.loads(text)

    # Thing Model → TD instance
    types = thing.get("@type", [])
    if isinstance(types, list) and "tm:ThingModel" in types:
        types.remove("tm:ThingModel")
        thing["@type"] = types[0] if len(types) == 1 else types
        version = thing.get("version", {})
        if "model" in version:
            version["instance"] = version.pop("model")
        if "id" not in thing and "NODE_ID" in values:
            thing["id"] = "urn:uuid:" + values["NODE_ID"]

    return thing
