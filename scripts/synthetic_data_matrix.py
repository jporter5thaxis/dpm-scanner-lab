"""Deterministic 10x10 ECC200 fixtures for software checks only."""
import cv2
import numpy as np
import zxingcpp


def symbol(module=4, rotation=0, condition='clean'):
    raw = np.asarray(zxingcpp.create_barcode('123456', zxingcpp.BarcodeFormat.DataMatrix)
                     .to_image(scale=1, add_quiet_zones=False))
    assert raw.shape == (10, 10), 'Fixture must really contain 10x10 cells'
    raw = np.pad(raw, 2, constant_values=255)
    raw = cv2.resize(raw, None, fx=module, fy=module, interpolation=cv2.INTER_NEAREST)
    if rotation:
        size = int(np.ceil(raw.shape[0]*1.5))
        matrix = cv2.getRotationMatrix2D((raw.shape[1]/2, raw.shape[0]/2), rotation, 1)
        matrix[:, 2] += (size-raw.shape[0])/2
        raw = cv2.warpAffine(raw, matrix, (size, size), borderValue=255)
    if condition == 'inverted':
        raw = 255-raw
    elif condition == 'low_contrast':
        raw = (raw*.18+110).astype(np.uint8)
    elif condition == 'blur':
        raw = cv2.GaussianBlur(raw, (3, 3), .6)
    return cv2.cvtColor(raw, cv2.COLOR_GRAY2BGR)


def scene(x=710, y=200, module=4, rotation=0, condition='clean', width=1280, height=720):
    image = np.full((height, width, 3), 170, np.uint8)
    code = symbol(module, rotation, condition)
    image[y:y+code.shape[0], x:x+code.shape[1]] = code
    return image


