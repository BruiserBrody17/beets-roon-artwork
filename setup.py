import setuptools

setuptools.setup(
    name="beets-roon-artwork",
    version="0.1.0",
    description="Strips embedded art and copies a source Artwork/ folder into "
                 "the destination, renamed to Roon's <category>-NN convention",
    packages=["beetsplug"],
    install_requires=["beets", "mutagen"],
)
