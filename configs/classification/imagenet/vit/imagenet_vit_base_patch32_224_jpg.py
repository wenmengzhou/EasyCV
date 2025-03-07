_base_ = './vit_base_patch16_224_b64x64_300e_jpg.py'
# model settings
model = dict(
    backbone=dict(
        type='PytorchImageModelWrapper',
        model_name='vit_base_patch32_224',
        num_classes=1000,
        pretrained=False,
    ))
