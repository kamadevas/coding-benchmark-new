import 'package:flutter/material.dart';

/// Status of an offline map country.
enum CountryStatus { planned, available, installed }

class CountryInfo {
  final String code;   // e.g. "DE", "FR", "IT"
  final String name;   // e.g. "Deutschland"
  final CountryStatus status;
  final int sizeMb;

  const CountryInfo({
    required this.code,
    required this.name,
    required this.status,
    this.sizeMb = 0,
  });

  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      other is CountryInfo &&
          code == other.code &&
          name == other.name &&
          status == other.status &&
          sizeMb == other.sizeMb;

  @override
  int get hashCode => code.hashCode ^ name.hashCode ^ status.hashCode ^ sizeMb.hashCode;
}

class OfflineCountryList extends StatelessWidget {
  final List<CountryInfo> countries;
  final void Function(CountryInfo country)? onDownload;
  final void Function(CountryInfo country)? onDelete;
  final void Function(CountryInfo country)? onOpen;

  const OfflineCountryList({
    super.key,
    required this.countries,
    this.onDownload,
    this.onDelete,
    this.onOpen,
  });

  @override
  Widget build(BuildContext context) {
    if (countries.isEmpty) {
      return const Center(child: Text('Keine Länder verfügbar'));
    }
    return ListView.builder(
      itemCount: countries.length,
      itemBuilder: (context, index) {
        final country = countries[index];
        return ListTile(
          leading: Text(
            country.code,
            style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 18),
          ),
          title: Text(country.name),
          subtitle: Text('${country.sizeMb} MB'),
          trailing: _buildAction(context, country),
        );
      },
    );
  }

  Widget _buildAction(BuildContext context, CountryInfo country) {
    // BUG 1: planned countries get a download button that is active (should be disabled/inactive)
    // BUG 2: available countries don't get a download button (wrong status check)
    // BUG 3: installed countries show wrong label/action
    switch (country.status) {
      case CountryStatus.planned:
        // BUG: Planned countries should NOT have an active download button
        return ElevatedButton(
          onPressed: () => onDownload?.call(country),
          child: const Text('Download'),
        );
      case CountryStatus.available:
        // BUG: Available countries should have download action, but the check
        //      in the switch doesn't work because of a fall-through or missing case
        return const Text('Verfügbar'); // BUG: just text, no button, no action possible
      case CountryStatus.installed:
        // BUG: Installed countries should show 'Öffnen' + 'Löschen', but show wrong label
        return const Text('Installiert'); // BUG: just text, no open/delete buttons
    }
  }
}

// Helper: Creates sample fixture country list for testing
List<CountryInfo> sampleCountries() {
  return const [
    CountryInfo(code: 'DE', name: 'Deutschland', status: CountryStatus.available, sizeMb: 450),
    CountryInfo(code: 'FR', name: 'Frankreich', status: CountryStatus.planned, sizeMb: 380),
    CountryInfo(code: 'IT', name: 'Italien', status: CountryStatus.installed, sizeMb: 320),
    CountryInfo(code: 'AT', name: 'Österreich', status: CountryStatus.available, sizeMb: 120),
    CountryInfo(code: 'CH', name: 'Schweiz', status: CountryStatus.planned, sizeMb: 95),
  ];
}
