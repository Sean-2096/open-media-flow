from open_media_flow.models import ContentTask, Platform
from open_media_flow.store import JsonTaskStore


def test_store_round_trip(tmp_path):
    store = JsonTaskStore(tmp_path / "tasks.json")
    task = ContentTask(topic="测试任务", platforms=[Platform.YOUTUBE])
    store.create(task)
    loaded = store.get(task.id)
    assert loaded.topic == "测试任务"
    assert store.list()[0].id == task.id

