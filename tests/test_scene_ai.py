from dataclasses import replace

from src.database import PhotoDatabase
from src.exif_reader import read_photo_info
from src.scene_ai import Evidence, MODEL_VERSION, Role, analyze_selection, rank_subjects


def test_building_with_tiny_visitor_and_portrait_in_front_of_building():
    church = {"building": Evidence(.29, .47, .65, .67),
              "person": Evidence(.22, .005, .006, .01),
              "sky": Evidence(.19, .23, .03, .04)}
    roles = rank_subjects(church, True)
    assert roles == {"building": Role.PRIMARY, "sky": Role.BACKGROUND, "person": Role.BACKGROUND}
    portrait = dict(church, person=Evidence(.26, .37, .62, .58), building=Evidence(.22, .18, .06, .24))
    roles = rank_subjects(portrait, True)
    assert roles["person"] == Role.PRIMARY
    assert roles["building"] < roles["person"]
    assert "person" not in rank_subjects(portrait, False)


def test_distant_city_flower_landscape_and_dual_subjects():
    city = {"city": Evidence(.28, .14, .31, .5), "building": Evidence(.26, .13, .30, .48)}
    assert rank_subjects(city, False) == {"city": Role.PRIMARY, "building": Role.SECONDARY}
    valley = {"landscape": Evidence(.28, .75, .72, .68), "flowers": Evidence(.23, .04, .08, .06)}
    assert rank_subjects(valley, False) == {"landscape": Role.PRIMARY, "flowers": Role.SECONDARY}
    blossoms = {"landscape": Evidence(.229, .60, .72, .68), "flowers": Evidence(.26, 0, 0, 0)}
    assert rank_subjects(blossoms, False)["flowers"] == Role.PRIMARY
    portrait = {"person": Evidence(.26, .27, .50, .42), "flowers": Evidence(.29, .33, .41, .41)}
    assert set(rank_subjects(portrait, True).values()) == {Role.PRIMARY}
    assert len(rank_subjects(portrait, True)) == 2
    assert rank_subjects({}, False) == {}


def test_scene_batch_caches_empty_results_and_invalidates_changed_files(tmp_path):
    path = tmp_path / "moon.jpg"
    path.write_bytes(b"fixture")
    info = read_photo_info(path, "jpeg")

    class EmptyScene:
        def analyze(self, _info):
            return {}

    options = dict(progress=lambda *_: None, cancelled=lambda: False, make_analyzer=EmptyScene)
    catalog = tmp_path / "library.db"
    with PhotoDatabase.open(catalog) as db:
        db.upsert_photo(info)
        assert analyze_selection([info], db, **options) == (1, 0, 0, None)
    with PhotoDatabase.open(catalog) as db:
        assert db.scene_results(MODEL_VERSION) == {str(path): {}}
        assert db.scene_results("other model") == {}
        assert analyze_selection([info], db, **options) == (0, 1, 0, None)
        changed = replace(info, file_mtime_ns=info.file_mtime_ns + 1)
        db.upsert_photo(changed)
        assert not db.save_scene_result(info, MODEL_VERSION, info.file_mtime_ns, {})
        assert db.scene_results(MODEL_VERSION) == {}
        assert db.save_scene_result(changed, MODEL_VERSION, changed.file_mtime_ns, {"sky": Role.PRIMARY})
        db.remove_photos([path])
        db.upsert_photo(changed)
        assert db.scene_results(MODEL_VERSION) == {}
