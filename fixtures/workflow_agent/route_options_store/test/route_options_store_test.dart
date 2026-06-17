import 'package:test/test.dart';
import 'package:route_options_store/route_options_store.dart';

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

  // 1. Default mode is fastest
  check('default mode is fastest', () {
    final opts = RouteOptions();
    return opts.mode == RouteMode.fastest;
  });

  // 2. Default avoid flags are all false
  check('default avoid flags all false', () {
    final opts = RouteOptions();
    return !opts.avoidFlags.avoidTolls &&
        !opts.avoidFlags.avoidHighways &&
        !opts.avoidFlags.avoidFerries;
  });

  // 3. Store and read 'shortest' mode
  check('store and read shortest mode', () {
    final opts = RouteOptions(mode: RouteMode.shortest);
    return opts.mode == RouteMode.shortest;
  });

  // 4. Store and read 'economic' mode
  check('store and read economic mode', () {
    final opts = RouteOptions(mode: RouteMode.economic);
    return opts.mode == RouteMode.economic;
  });

  // 5. toJson and fromJson roundtrip preserves mode
  check('toJson and fromJson roundtrip mode', () {
    final opts = RouteOptions(mode: RouteMode.shortest);
    final json = opts.toJson();
    final restored = RouteOptions.fromJson(json);
    return restored.mode == RouteMode.shortest;
  });

  // 6. toJson and fromJson roundtrip preserves avoidFlags
  check('toJson and fromJson roundtrip avoidFlags', () {
    final opts = RouteOptions(
      avoidFlags: const AvoidFlags(
        avoidTolls: true,
        avoidHighways: false,
        avoidFerries: true,
      ),
    );
    final json = opts.toJson();
    final restored = RouteOptions.fromJson(json);
    return restored.avoidFlags.avoidTolls == true &&
        restored.avoidFlags.avoidHighways == false &&
        restored.avoidFlags.avoidFerries == true;
  });

  // 7. fromJson with unknown mode defaults to fastest
  check('fromJson unknown mode defaults to fastest', () {
    final json = {'mode': 'racing', 'avoidFlags': {}};
    try {
      final opts = RouteOptions.fromJson(json);
      return opts.mode == RouteMode.fastest;
    } catch (e) {
      return false; // must not crash
    }
  });

  // 8. fromJson handles missing mode key gracefully
  check('fromJson handles missing mode key', () {
    final json = <String, dynamic>{'avoidFlags': {}};
    try {
      final opts = RouteOptions.fromJson(json);
      return opts.mode == RouteMode.fastest;
    } catch (e) {
      return false; // must not crash
    }
  });

  // 9. fromJson handles completely empty JSON
  check('fromJson handles empty JSON', () {
    final json = <String, dynamic>{};
    try {
      final opts = RouteOptions.fromJson(json);
      return opts.mode == RouteMode.fastest &&
          !opts.avoidFlags.avoidTolls;
    } catch (e) {
      return false; // must not crash
    }
  });

  // 10. copyWith preserves unchanged avoidFlags
  check('copyWith preserves unchanged avoidFlags', () {
    final opts = RouteOptions(
      mode: RouteMode.economic,
      avoidFlags: const AvoidFlags(avoidTolls: true),
    );
    final copy = opts.copyWith(mode: RouteMode.shortest);
    return copy.mode == RouteMode.shortest &&
        copy.avoidFlags.avoidTolls == true; // should keep existing flag
  });

  print('PASSED:$passed/$total');
}
