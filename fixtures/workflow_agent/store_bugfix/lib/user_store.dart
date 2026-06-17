class User {
  final String id;
  final String name;
  final String email;

  const User({required this.id, required this.name, required this.email});

  Map<String, dynamic> toJson() => {'id': id, 'name': name, 'email': email};

  factory User.fromJson(Map<String, dynamic> json) => User(
    id: json['id'] as String,
    name: json['name'] as String,
    email: json['email'] as String,
  );

  User copyWith({String? id, String? name, String? email}) =>
      User(id: id ?? this.id, name: name ?? this.name, email: email ?? this.email);

  @override
  bool operator ==(Object other) =>
      identical(this, other) || other is User && id == other.id && name == other.name && email == other.email;

  @override
  int get hashCode => id.hashCode ^ name.hashCode ^ email.hashCode;

  @override
  String toString() => 'User(id: $id, name: $name, email: $email)';
}

class UserStore {
  final List<User> _users;

  UserStore([List<User>? initialUsers]) : _users = List<User>.of(initialUsers ?? const []);

  List<User> get users => List<User>.unmodifiable(_users);

  void add(User user) => _users.add(user);

  void remove(String id) => _users.removeWhere((u) => u.id == id);

  User? findById(String id) {
    for (final user in _users) {
      if (user.id == id) return user;
    }
    return null;
  }

  UserStore fromJson(Map<String, dynamic> json) {
    // BUG: json['users'] kann null sein, was zu einem Laufzeitfehler führt
    // BUG: Zweites Problem: fromJson gibt eine NEUE Instanz statt this zu modifizieren
    // AUFGABE: Repariere diese Methode so, dass sie das Store-Objekt korrekt befüllt
    //          und null-sicher arbeitet. Tests müssen danach grün sein.
    final List<dynamic> rawUsers = json['users'];
    _users.clear();
    for (final raw in rawUsers) {
      _users.add(User.fromJson(raw as Map<String, dynamic>));
    }
    return this;
  }

  Map<String, dynamic> toJson() => {
    'users': _users.map((u) => u.toJson()).toList(),
  };
}
