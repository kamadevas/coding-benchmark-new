/// Possible route modes.
enum RouteMode { fastest, shortest, economic }

/// Avoid flags for route calculation.
class AvoidFlags {
  final bool avoidTolls;
  final bool avoidHighways;
  final bool avoidFerries;

  const AvoidFlags({
    this.avoidTolls = false,
    this.avoidHighways = false,
    this.avoidFerries = false,
  });

  Map<String, dynamic> toJson() => {
        'avoidTolls': avoidTolls,
        'avoidHighways': avoidHighways,
        'avoidFerries': avoidFerries,
      };

  factory AvoidFlags.fromJson(Map<String, dynamic> json) => AvoidFlags(
        avoidTolls: json['avoidTolls'] as bool? ?? false,
        avoidHighways: json['avoidHighways'] as bool? ?? false,
        avoidFerries: json['avoidFerries'] as bool? ?? false,
      );

  AvoidFlags copyWith({bool? avoidTolls, bool? avoidHighways, bool? avoidFerries}) =>
      AvoidFlags(
        avoidTolls: avoidTolls ?? this.avoidTolls,
        avoidHighways: avoidHighways ?? this.avoidHighways,
        avoidFerries: avoidFerries ?? this.avoidFerries,
      );

  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      other is AvoidFlags &&
          avoidTolls == other.avoidTolls &&
          avoidHighways == other.avoidHighways &&
          avoidFerries == other.avoidFerries;

  @override
  int get hashCode => avoidTolls.hashCode ^ avoidHighways.hashCode ^ avoidFerries.hashCode;
}

class RouteOptions {
  RouteMode mode;
  AvoidFlags avoidFlags;

  RouteOptions({
    this.mode = RouteMode.fastest,
    AvoidFlags? avoidFlags,
  }) : avoidFlags = avoidFlags ?? const AvoidFlags();

  // BUG 1: fromJson doesn't handle missing 'mode' key gracefully – crashes with null error
  // BUG 2: Avoid flags serialization is broken: 'avoidFlags' is stored as raw Map
  //         instead of calling AvoidFlags.fromJson, but toJson calls avoidFlags.toJson()
  //         consistently. The bug is in fromJson which stores the raw Map.
  // BUG 3: Unknown mode values (e.g. 'racing') are not defaulted to 'fastest',
  //         causing a crash on RouteMode.values.byName().
  // BUG 4: copyWith overwrites avoidFlags entirely instead of merging with existing flags.

  Map<String, dynamic> toJson() => {
        'mode': mode.name,
        'avoidFlags': avoidFlags.toJson(),
      };

  factory RouteOptions.fromJson(Map<String, dynamic> json) {
    // BUG 1+3: No null check on json['mode'], no try-catch on byName()
    final modeStr = json['mode'] as String;
    final mode = RouteMode.values.byName(modeStr);

    // BUG 2: Raw map stored instead of AvoidFlags.fromJson
    // BUG 4: Later, copyWith also has a bug (see below)
    return RouteOptions(
      mode: mode,
      avoidFlags: AvoidFlags(
        avoidTolls: json['avoidFlags']['avoidTolls'] as bool? ?? false,
        avoidHighways: json['avoidFlags']['avoidHighways'] as bool? ?? false,
        avoidFerries: json['avoidFlags']['avoidFerries'] as bool? ?? false,
      ),
    );
  }

  // BUG 4: copyWith sets ALL fields from input, so if you only pass mode,
  //        avoidFlags gets reset to defaults (empty AvoidFlags()).
  RouteOptions copyWith({RouteMode? mode, AvoidFlags? avoidFlags}) {
    return RouteOptions(
      mode: mode ?? this.mode,
      avoidFlags: avoidFlags ?? const AvoidFlags(), // BUG: should be `?? this.avoidFlags`
    );
  }
}
