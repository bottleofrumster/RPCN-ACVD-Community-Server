import pytest

from acvd_server.protocol import decode_proto, first_field
from tools.extract_offline_inventory import (
    FIELD1_FOR_CLASS,
    INVENTORY_COUNT_OFFSET,
    INVENTORY_RECORD_SIZE,
    INVENTORY_RECORDS_OFFSET,
    extract_inventory,
)


def _gprog(records: list[tuple[int, int, int, int]]) -> bytes:
    size = INVENTORY_RECORDS_OFFSET + len(records) * INVENTORY_RECORD_SIZE
    data = bytearray(size)
    data[INVENTORY_COUNT_OFFSET : INVENTORY_COUNT_OFFSET + 4] = len(records).to_bytes(4, "big")
    for index, (class_code, part_id, tune_bits, instance_id) in enumerate(records):
        offset = INVENTORY_RECORDS_OFFSET + index * INVENTORY_RECORD_SIZE
        data[offset] = class_code
        data[offset + 1] = 0x88 if class_code in (10, 11) else 0xC8
        data[offset + 2 : offset + 4] = part_id.to_bytes(2, "big")
        data[offset + 4 : offset + 8] = tune_bits.to_bytes(4, "big")
        data[offset + 8 : offset + 16] = instance_id.to_bytes(8, "big")
    return bytes(data)


def test_extract_inventory_maps_classes_tuning_and_instances() -> None:
    records = [
        (class_code, 100 + index, tune_bits, 0xE2000000000000 + index)
        for index, (class_code, tune_bits) in enumerate(
            zip(
                FIELD1_FOR_CLASS,
                (
                    0,
                    0x30000000,
                    0x00300000,
                    0x20100000,
                    0x03000000,
                    0x21000000,
                    0x11100000,
                    0x01200000,
                    0x10200000,
                    0,
                    0,
                    0,
                ),
                strict=True,
            )
        )
    ]
    stored, metadata = extract_inventory(_gprog(records))
    decoded = decode_proto(stored)
    assert len(decoded) == len(records)
    expected_tunes = [1000, 1300, 1003, 1201, 1030, 1210, 1111, 1012, 1102, 1000, 1000, 1000]
    for index, field in enumerate(decoded):
        item = decode_proto(field.value)
        class_code, part_id, _, instance_id = records[index]
        assert [first_field(item, number).value for number in range(1, 6)] == [
            FIELD1_FOR_CLASS[class_code],
            part_id,
            class_code,
            instance_id,
            expected_tunes[index],
        ]
    assert metadata["record_count"] == len(records)
    assert metadata["nonzero_instance_count"] == len(records)
    assert metadata["tuned_count"] == 8


def test_extract_inventory_rejects_unknown_class_and_truncation() -> None:
    with pytest.raises(ValueError, match="unknown class code"):
        extract_inventory(_gprog([(8, 110, 0, 1)]))
    truncated = bytearray(INVENTORY_RECORDS_OFFSET)
    truncated[INVENTORY_COUNT_OFFSET : INVENTORY_COUNT_OFFSET + 4] = (1).to_bytes(4, "big")
    with pytest.raises(ValueError, match="inventory needs"):
        extract_inventory(bytes(truncated))
