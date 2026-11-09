from setuptools import setup, find_packages

setup(
    name="kekesi",
    version="2.0.0",
    packages=find_packages(),
    py_modules=["main"],
    package_data={
        "kekesi": ["*.yaml", "*.json", "*.txt"],
    },
    install_requires=[
        # 这里列出主要依赖
    ],
    entry_points={
        "console_scripts": [
            "kekesi=main:main",
        ],
    },
)