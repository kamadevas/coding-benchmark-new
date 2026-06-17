import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:offline_country_list/offline_country_list.dart';

void main() {
  const total = 8;

  testWidgets('OfflineCountryList behaviour', (tester) async {
    var passed = 0;

    Future<void> check(String name, Future<void> Function() body) async {
      try {
        await body();
        passed += 1;
      } catch (error, stackTrace) {
        // ignore: avoid_print
        print('CHECK_FAILED:$name:$error');
        // ignore: avoid_print
        print(stackTrace.toString().split('\n').take(3).join('\n'));
      } finally {
        await tester.pumpAndSettle();
        await tester.pumpWidget(const SizedBox.shrink());
        await tester.pump();
      }
    }

    Future<void> pumpList({
      List<CountryInfo>? countries,
      void Function(CountryInfo)? onDownload,
      void Function(CountryInfo)? onDelete,
      void Function(CountryInfo)? onOpen,
    }) async {
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: OfflineCountryList(
            countries: countries ?? sampleCountries(),
            onDownload: onDownload,
            onDelete: onDelete,
            onOpen: onOpen,
          ),
        ),
      ));
      await tester.pump();
    }

    // 1. Germany/DE is visible and recognizable
    await check('germany DE is visible', () async {
      await pumpList();
      expect(find.text('DE'), findsOneWidget);
      expect(find.text('Deutschland'), findsOneWidget);
    });

    // 2. All sample countries are visible
    await check('all sample countries visible', () async {
      await pumpList();
      expect(find.text('Deutschland'), findsOneWidget);
      expect(find.text('Frankreich'), findsOneWidget);
      expect(find.text('Italien'), findsOneWidget);
      expect(find.text('Österreich'), findsOneWidget);
      expect(find.text('Schweiz'), findsOneWidget);
    });

    // 3. Planned countries must NOT have an active download button
    await check('planned country has no active download button', () async {
      await pumpList();
      // FR and CH are planned – neither may show an active download button
      for (final code in ['FR', 'CH']) {
        final tile = find.ancestor(
          of: find.text(code),
          matching: find.byType(ListTile),
        );
        expect(
          find.descendant(
            of: tile,
            matching: find.widgetWithText(ElevatedButton, 'Download'),
          ),
          findsNothing,
        );
      }
    });

    // 4. Available country must have a download button that triggers callback
    await check('available country triggers download callback', () async {
      CountryInfo? downloaded;
      await pumpList(onDownload: (c) => downloaded = c);

      // DE is available – must show a download button
      final tile = find.ancestor(
        of: find.text('DE'),
        matching: find.byType(ListTile),
      );
      final downloadButton = find.descendant(
        of: tile,
        matching: find.widgetWithText(ElevatedButton, 'Download'),
      );
      expect(downloadButton, findsOneWidget);

      await tester.tap(downloadButton);
      await tester.pump();
      expect(downloaded, isNotNull);
      expect(downloaded!.code, 'DE');
    });

    // 5. Installed country must show Öffnen/Löschen and fire callbacks
    await check('installed country has open and delete actions', () async {
      CountryInfo? opened;
      CountryInfo? deleted;
      await pumpList(
        onOpen: (c) => opened = c,
        onDelete: (c) => deleted = c,
      );

      // IT is installed
      final tile = find.ancestor(
        of: find.text('IT'),
        matching: find.byType(ListTile),
      );

      final openButton = find.descendant(
        of: tile,
        matching: find.text('Öffnen'),
      );
      expect(openButton, findsOneWidget);

      final deleteButton = find.descendant(
        of: tile,
        matching: find.text('Löschen'),
      );
      expect(deleteButton, findsOneWidget);

      await tester.tap(openButton);
      await tester.pump();
      expect(opened, isNotNull);
      expect(opened!.code, 'IT');

      await tester.tap(deleteButton);
      await tester.pump();
      expect(deleted, isNotNull);
      expect(deleted!.code, 'IT');
    });

    // 6. Empty list shows placeholder
    await check('empty list shows placeholder', () async {
      await pumpList(countries: const []);
      expect(find.text('Keine Länder verfügbar'), findsOneWidget);
    });

    // 7. Country sizes are displayed
    await check('country sizes displayed', () async {
      await pumpList();
      expect(find.textContaining('450'), findsOneWidget); // DE
      expect(find.textContaining('380'), findsOneWidget); // FR
    });

    // 8. No fake production claim about installed maps
    await check('no fake production claim about real map', () async {
      await pumpList();
      expect(find.textContaining('echte Deutschlandkarte'), findsNothing);
      expect(find.textContaining('production map'), findsNothing);
      expect(find.textContaining('Produktionskarte'), findsNothing);
    });

    print('PASSED:$passed/$total');
  });
}
