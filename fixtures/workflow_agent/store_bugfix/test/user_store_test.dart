import 'package:test/test.dart';
import 'package:store_bugfix/user_store.dart';

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
      // intentionally empty - check fails
    }
  }

  check('add and find user', () {
    final store = UserStore();
    store.add(const User(id: '1', name: 'Alice', email: 'alice@example.com'));
    final found = store.findById('1');
    return found != null && found.name == 'Alice';
  });

  check('remove user', () {
    final store = UserStore();
    store.add(const User(id: '1', name: 'Bob', email: 'bob@example.com'));
    store.remove('1');
    return store.users.isEmpty;
  });

  check('toJson and fromJson roundtrip', () {
    final store = UserStore();
    store.add(const User(id: '1', name: 'Carol', email: 'carol@example.com'));
    final json = store.toJson();
    final store2 = UserStore().fromJson(json);
    return store2.users.length == 1 && store2.users.first.name == 'Carol';
  });

  check('fromJson with multiple users', () {
    final json = {
      'users': [
        {'id': '1', 'name': 'User1', 'email': 'u1@example.com'},
        {'id': '2', 'name': 'User2', 'email': 'u2@example.com'},
      ]
    };
    final store = UserStore().fromJson(json);
    return store.users.length == 2 && store.users[1].id == '2';
  });

  check('fromJson with empty users list', () {
    final json = {'users': <Map<String, dynamic>>[]};
    final store = UserStore().fromJson(json);
    return store.users.isEmpty;
  });

  check('fromJson handles null users key gracefully', () {
    final json = <String, dynamic>{};
    try {
      UserStore().fromJson(json);
      return true; // no crash = pass
    } catch (e) {
      return false;
    }
  });

  check('fromJson does not retain previous users', () {
    final store = UserStore();
    store.add(const User(id: 'old', name: 'Old', email: 'old@example.com'));
    final json = {
      'users': [
        {'id': 'new', 'name': 'New', 'email': 'new@example.com'},
      ]
    };
    store.fromJson(json);
    return store.users.length == 1 && store.users.first.id == 'new';
  });

  check('fromJson returns same instance', () {
    final store = UserStore();
    final json = {
      'users': [
        {'id': 'x', 'name': 'X', 'email': 'x@example.com'},
      ]
    };
    final result = store.fromJson(json);
    return identical(store, result);
  });

  check('toJson produces valid structure', () {
    final store = UserStore();
    store.add(const User(id: '1', name: 'Test', email: 'test@example.com'));
    final json = store.toJson();
    return json.containsKey('users') && json['users'] is List && (json['users'] as List).length == 1;
  });

  print('PASSED:$passed/$total');
}
