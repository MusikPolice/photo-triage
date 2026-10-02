"""The only module allowed to mutate files: trash, restore, purge, and EXIF writes.

Other packages use the public interface exported here, never the submodules.
See docs/dev-environment.md section 7.
"""
