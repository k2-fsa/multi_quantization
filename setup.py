#!/usr/bin/env python3

from setuptools import find_packages, setup
from pathlib import Path

quantization_dir = Path(__file__).parent
install_requires = (quantization_dir / "requirements.txt").read_text().splitlines()

setup(
    name="multi_quantization",
    version="0.1",
    python_requires=">=3.6.0",
    description="Utility for learning compact codes for vectors, based on Torch",
    author="Daniel Povey",
    author_email='dpovey@gmail.com',
    license="Apache-2.0 License",
    url="https://github.com/k2-fsa/multi_quantization",
    packages=find_packages(),
    package_data={
        "":["requirements.txt"]
    },
    install_requires=install_requires,
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Intended Audience :: Science/Research",
        "Operating System :: POSIX :: Linux",
        "License :: OSI Approved :: Apache Software License",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Typing :: Typed",
    ],
)
