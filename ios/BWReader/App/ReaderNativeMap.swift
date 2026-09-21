import SwiftUI
import MapKit

struct ReaderNativeMapData {
    let latitude: Double
    let longitude: Double
    let zoom: Double
    let markers: [CLLocationCoordinate2D]

    init?(_ value: [String: Any]) {
        guard let lat = (value["lat"] as? NSNumber)?.doubleValue,
              let lon = (value["lon"] as? NSNumber)?.doubleValue,
              lat.isFinite, lon.isFinite, abs(lat) <= 90, abs(lon) <= 180 else { return nil }
        latitude = lat; longitude = lon
        let zoom = (value["zoom"] as? NSNumber)?.doubleValue ?? 5
        self.zoom = zoom.isFinite ? min(19, max(2, zoom)) : 5
        markers = (value["marks"] as? [[NSNumber]] ?? []).compactMap { point in
            guard point.count == 2 else { return nil }
            let lat = point[0].doubleValue, lon = point[1].doubleValue
            guard lat.isFinite, lon.isFinite, abs(lat) <= 90, abs(lon) <= 180 else { return nil }
            return CLLocationCoordinate2D(latitude: lat, longitude: lon)
        }
    }

    var region: MKCoordinateRegion {
        let span = min(160, 360 / pow(2, zoom - 1))
        return MKCoordinateRegion(center: CLLocationCoordinate2D(latitude: latitude, longitude: longitude),
                                  span: MKCoordinateSpan(latitudeDelta: span, longitudeDelta: span))
    }
}

@MainActor
struct ReaderNativeMap: View {
    let data: ReaderNativeMapData
    let interactive: Bool
    @State private var position: MapCameraPosition = .automatic

    var body: some View {
        Map(position: $position, interactionModes: interactive ? .all : []) {
            ForEach(data.markers.indices, id: \.self) { index in
                Marker(data.markers.count > 1 ? "\(index + 1)" : "位置", coordinate: data.markers[index])
                    .tint(ReaderNativeTheme.accent)
            }
        }
        .mapControls {
            if interactive { MapCompass(); MapScaleView() }
        }
        .overlay(alignment: .bottomTrailing) {
            if interactive {
                Button("回到标记", systemImage: "scope") {
                    withAnimation { position = .region(data.region) }
                }.buttonStyle(.borderedProminent).padding()
            }
        }
        .onAppear { position = .region(data.region) }
    }
}
