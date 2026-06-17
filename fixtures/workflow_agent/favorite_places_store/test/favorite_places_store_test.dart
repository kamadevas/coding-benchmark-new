import 'package:test/test.dart';
import 'package:favorite_places_store/favorite_places_store.dart';

void main() {
  var passed = 0;
  var total = 0;

  void check(String name, bool Function() body) {
    total += 1;
    try {
      if (body()) {
        passed += 1;
      }
    } catch (e) {
      // intentionally empty
    }
  }

  // 1. Add a place and find it
  check('add and find place', () {
    final store = FavoritePlacesStore();
    store.add(const Place(id: 'p1', name: 'Home', latitude: 52.52, longitude: 13.405));
    final found = store.findById('p1');
    return found != null && found.name == 'Home';
  });

  // 2. Same name + same coords is not added twice
  check('same name and coords not added twice', () {
    final store = FavoritePlacesStore();
    store.add(const Place(id: 'p1', name: 'Home', latitude: 52.52, longitude: 13.405));
    store.add(const Place(id: 'p2', name: 'Home', latitude: 52.52, longitude: 13.405));
    return store.places.length == 1; // should be deduplicated
  });

  // 3. Different names at same coords are allowed
  check('different names at same coords allowed', () {
    final store = FavoritePlacesStore();
    store.add(const Place(id: 'p1', name: 'Cafe A', latitude: 48.8566, longitude: 2.3522));
    store.add(const Place(id: 'p2', name: 'Cafe B', latitude: 48.8566, longitude: 2.3522));
    return store.places.length == 2;
  });

  // 4. Delete by ID works
  check('delete by id works', () {
    final store = FavoritePlacesStore();
    store.add(const Place(id: 'delete_me', name: 'Temp', latitude: 0, longitude: 0));
    store.remove('delete_me');
    return store.places.isEmpty;
  });

  // 5. Delete by non-existent ID is safe
  check('delete non-existent id is safe', () {
    final store = FavoritePlacesStore();
    store.add(const Place(id: 'keep', name: 'Keep', latitude: 1, longitude: 1));
    store.remove('no_such_id');
    return store.places.length == 1;
  });

  // 6. JSON import works
  check('json import works', () {
    final store = FavoritePlacesStore();
    store.importJson(
      '[{"id":"a","name":"Place A","latitude":1.0,"longitude":2.0},'
      '{"id":"b","name":"Place B","latitude":3.0,"longitude":4.0}]',
    );
    return store.places.length == 2 && store.findById('a') != null;
  });

  // 7. JSON import ignores broken entries
  check('json import ignores broken entries', () {
    final store = FavoritePlacesStore();
    try {
      store.importJson(
        '[{"id":"ok","name":"OK","latitude":0,"longitude":0},'
        'null,'
        '{"id":"also_ok","name":"Also OK","latitude":1,"longitude":1}]',
      );
      // Should import the two valid entries, skip null
      return store.places.length == 2 &&
          store.findById('ok') != null &&
          store.findById('also_ok') != null;
    } catch (e) {
      return false; // must not crash
    }
  });

  // 8. JSON import with empty array
  check('json import empty array', () {
    final store = FavoritePlacesStore();
    store.add(const Place(id: 'pre', name: 'Pre', latitude: 0, longitude: 0));
    store.importJson('[]');
    return store.places.isEmpty;
  });

  // 9. Order remains stable after operations
  check('order remains stable', () {
    final store = FavoritePlacesStore();
    store.add(const Place(id: '1', name: 'First', latitude: 10, longitude: 10));
    store.add(const Place(id: '2', name: 'Second', latitude: 20, longitude: 20));
    store.add(const Place(id: '3', name: 'Third', latitude: 30, longitude: 30));
    store.remove('2');
    return store.places.length == 2 &&
        store.places[0].id == '1' &&
        store.places[1].id == '3';
  });

  // 10. toJsonString produces valid JSON
  check('toJsonString produces valid JSON', () {
    final store = FavoritePlacesStore();
    store.add(const Place(id: 'x', name: 'X', latitude: 1, longitude: 2));
    final str = store.toJsonString();
    return str.contains('"id":"x"') && str.contains('"name":"X"');
  });

  print('PASSED:$passed/$total');
}
