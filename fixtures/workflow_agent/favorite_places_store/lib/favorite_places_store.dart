import 'dart:convert';

class Place {
  final String id;
  final String name;
  final double latitude;
  final double longitude;

  const Place({
    required this.id,
    required this.name,
    required this.latitude,
    required this.longitude,
  });

  Map<String, dynamic> toJson() => {
        'id': id,
        'name': name,
        'latitude': latitude,
        'longitude': longitude,
      };

  factory Place.fromJson(Map<String, dynamic> json) => Place(
        id: json['id'] as String,
        name: json['name'] as String,
        latitude: (json['latitude'] as num).toDouble(),
        longitude: (json['longitude'] as num).toDouble(),
      );

  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      other is Place &&
          id == other.id &&
          name == other.name &&
          latitude == other.latitude &&
          longitude == other.longitude;

  @override
  int get hashCode => id.hashCode ^ name.hashCode ^ latitude.hashCode ^ longitude.hashCode;
}

class FavoritePlacesStore {
  final List<Place> _places;
  int _nextId = 1;

  FavoritePlacesStore([List<Place>? initialPlaces])
      : _places = List<Place>.of(initialPlaces ?? const []);

  List<Place> get places => List<Place>.unmodifiable(_places);

  // BUG 1: add() doesn't check for duplicates (same name + same coords)
  void add(Place place) {
    _places.add(place);
  }

  // BUG 2: remove() compares wrong field – should compare place.id but doesn't work
  //        because it uses index-based removal incorrectly (or compares wrong thing).
  //        Actually: remove() works correctly here. Let's make a different bug:
  //        remove() by ID doesn't find the place when there are multiple with same name.
  void remove(String id) {
    _places.removeWhere((p) => p.name == id); // BUG: compares name instead of id!
  }

  // BUG 3: importJson doesn't skip broken entries – crashes on invalid data
  void importJson(String jsonStr) {
    final List<dynamic> rawList = jsonDecode(jsonStr) as List<dynamic>;
    _places.clear();
    for (final raw in rawList) {
      // BUG: No try-catch, no null check – crashes if entry is null or missing fields
      _places.add(Place.fromJson(raw as Map<String, dynamic>));
    }
  }

  // BUG 4: When importing, _nextId is reset to 1, so new places get duplicate IDs
  //        (stable IDs lost). This is correct behavior here but the test expects
  //        it to continue from the highest imported ID.

  String toJsonString() {
    return jsonEncode(_places.map((p) => p.toJson()).toList());
  }

  Place? findById(String id) {
    for (final place in _places) {
      if (place.id == id) return place;
    }
    return null;
  }
}
