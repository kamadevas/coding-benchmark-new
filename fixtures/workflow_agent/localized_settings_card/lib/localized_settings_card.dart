import 'package:flutter/material.dart';

/// Simple localization map for settings UI.
const Map<String, String> _defaultLabels = {
  'title': 'Einstellungen',
  'language': 'Sprache',
  'notifications': 'Benachrichtigungen',
  'save': 'Speichern',
  'cancel': 'Abbrechen',
};

class LocalizedSettingsCard extends StatefulWidget {
  final void Function(String language, bool notifications) onSave;
  final Map<String, String>? labels;

  const LocalizedSettingsCard({
    super.key,
    required this.onSave,
    this.labels,
  });

  @override
  State<LocalizedSettingsCard> createState() => _LocalizedSettingsCardState();
}

class _LocalizedSettingsCardState extends State<LocalizedSettingsCard> {
  String _selectedLanguage = 'Deutsch';
  bool _notificationsEnabled = true;
  final List<String> _languages = ['Deutsch', 'Englisch', 'Französisch'];

  Map<String, String> get _labels => widget.labels ?? _defaultLabels;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              _labels['title'] ?? 'Einstellungen',
              style: Theme.of(context).textTheme.titleLarge,
            ),
            const SizedBox(height: 16),
            // Language dropdown
            DropdownButtonFormField<String>(
              value: _selectedLanguage,
              decoration: InputDecoration(labelText: _labels['language'] ?? 'Sprache'),
              items: _languages
                  .map((lang) => DropdownMenuItem(value: lang, child: Text(lang)))
                  .toList(),
              onChanged: (value) {
                // BUG 1: setState fehlt – Wert wird zwar gesetzt, aber UI aktualisiert nicht
                _selectedLanguage = value ?? 'Deutsch';
              },
            ),
            const SizedBox(height: 16),
            // Notifications switch
            // BUG 2: SwitchListTile hat nur hardcoded deutschen Text 'Benachrichtigungen',
            //        ignoriert die _labels map
            SwitchListTile(
              title: const Text('Benachrichtigungen'), // BUG: hardcoded German
              value: _notificationsEnabled,
              onChanged: (value) {
                // BUG 3: setState fehlt auch hier
                _notificationsEnabled = value;
              },
              contentPadding: EdgeInsets.zero,
            ),
            const SizedBox(height: 16),
            // BUG 4: Langer Beschreibungstext hat kein softWrap/overflow handling
            const Text(
              'Diese Einstellungen beeinflussen die Darstellung und das Verhalten der gesamten Anwendungsoberfläche sowie aller Unterkomponenten.',
              style: TextStyle(fontSize: 12),
            ),
            const SizedBox(height: 16),
            Row(
              mainAxisAlignment: MainAxisAlignment.end,
              children: [
                TextButton(
                  onPressed: () => Navigator.of(context).pop(),
                  child: Text(_labels['cancel'] ?? 'Abbrechen'),
                ),
                const SizedBox(width: 8),
                ElevatedButton(
                  onPressed: () {
                    // BUG 5: Hartcodierte Werte statt _selectedLanguage / _notificationsEnabled
                    widget.onSave('Deutsch', false);
                    Navigator.of(context).pop();
                  },
                  child: Text(_labels['save'] ?? 'Speichern'),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}
