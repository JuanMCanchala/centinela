# Informe de integridad del corpus clinico

Generado automaticamente por `scripts/build_index.py`. No se edita a mano.

## Resumen de ingesta

| Metrica | Valor |
|---|---:|
| PDFs encontrados | 107 |
| Documentos ingeridos | 106 |
| Paginas procesadas | 2084 |
| Chunks indexados | 6667 |
| Duplicados omitidos | 2 |
| Documentos que requirieron OCR | 17 |
| Documentos sin texto util | 0 |
| Modelo de embeddings | `intfloat/multilingual-e5-large` (1024d) |
| Tiempo de construccion | 0 s |

## Defectos detectados en el material entregado

### Duplicados logicos

Mismo contenido con nombre de archivo y bytes distintos, asi que ningun hash de
archivo los detecta. Se descartan comparando una huella del texto normalizado.

| Documento omitido | Duplicado de | Carpeta |
|---|---|---|
| `Postoperative Pain Management in Total Knee Arthroplasty.pdf` | `Orthopaedic Surgery - 2019 - Li - Postoperative Pain Management in Total Knee Arthroplasty.pdf` | total joint replacement |
| `ecommendations for follow‑up of colorectal cancer survivors.pdf` | `Recommendations for follow‑up of colorectal cancer survivors.pdf` | colorectal cancer |

### Documentos sin capa de texto (resueltos con OCR)

| Documento | Carpeta | Paginas por OCR |
|---|---|---:|
| `Acute Appendicitis Evidence Based Medicine Guideline.pdf` | Appendicitis | 1/7 |
| `Apendicitis.pdf` | Appendicitis | 1/12 |
| `BOG303-Raul-Murillo.pdf` | breast_cancer | 6/23 |
| `Colelitiasis y colecistitis aguda.pdf` | cholecystitis | 1/15 |
| `Colon Cancer Surgery and Recovery.pdf` | colorectal cancer | 2/24 |
| `Diagnóstico y tratamiento del paciente con colecistitis aguda calculosa en el Hospital Universitario Nacional de Colombia .pdf` | cholecystitis | 2/110 |
| `Diagnóstico, estadificación, tratamiento y seguimiento del paciente adulto con cáncer de colon en el Hospital Universitario Nacional de Colombia.pdf` | colorectal cancer | 1/85 |
| `GUÍA DE MANEJO PARA EL DIAGNÓSTICO, TRATAMIENTO, SEGUIMIENTO Y PALIACIÓN DEL CÁNCER DE COLON Y RECTO.pdf` | colorectal cancer | 14/269 |
| `Identificación de microorganismos intrabdominales asociados a colección intrabdominal postoperatoria en niños con apendicitis perforada.pdf` | Appendicitis | 1/53 |
| `PLAN CASERO REEMPLAZO TOTAL DE RODILLA.pdf` | total joint replacement | 1/8 |
| `PLAN DE CUIDADO COLECISTECTOMIA.pdf` | cholecystitis | 1/5 |
| `PROGRAMA DE PROFUNDIZACIÓN Y ENTRENAMIENTO AVANZADO EN REEMPLAZO ARTICULAR.pdf` | total joint replacement | 1/9 |
| `Post-operative Surveillance Following Curative Resection of Colorectal Cancer in the Elderly Population in the United Kingdom- An Observational Study.pdf` | colorectal cancer | 1/10 |
| `REVISIÓN DE LA LITERATURA SOBRE LAAPENDICITIS AGUDA PEDIATRICA NO ESPECIFICADA EN EL PERI000 2000-2021.pdf` | Appendicitis | 1/1 |
| `Reemplazo total de Cadera Guía para pacientes y cuidadores El camino hacia su recuperación.pdf` | total joint replacement | 1/55 |
| `Reemplazo total de cadera Guía para el paciente .pdf` | total joint replacement | 2/28 |
| `cervix16nov-full.pdf` | breast_cancer | 7/86 |

### Documentos cuyo contenido no corresponde a su carpeta

**Este es el defecto con consecuencias clinicas.** Un RAG que enrute por nombre
de carpeta serviria estos documentos como si trataran del procedimiento de la
carpeta. Centinela clasifica el tema por el texto del documento, no por su
ubicacion, y la compuerta de fundamentacion se niega a responder cuando el
corpus no cubre el procedimiento del paciente.

| Documento | Carpeta dice | El texto dice |
|---|---|---|
| `002-GUIA-DE-CANCER-DE-CUELLO-UTERINO.pdf` | cancer_mama | **cancer_cuello_uterino** |
| `1-s2.0-S0360301625060390-main.pdf` | cancer_mama | **cancer_cuello_uterino** |
| `1-s2.0-S1048891X24221824-main.pdf` | cancer_mama | **cancer_cuello_uterino** |
| `BOG303-Raul-Murillo.pdf` | cancer_mama | **cancer_cuello_uterino** |
| `Cáncer-de-Cuello-Uterino-mar_2022.pdf` | cancer_mama | **cancer_cuello_uterino** |
| `Documento.pdf` | cancer_mama | **cancer_cuello_uterino** |
| `Factors-associated-with-delays-in-time-to-treatment-initiati` | cancer_mama | **cancer_cuello_uterino** |
| `Intl J Gynecology   Obste - 2025 - Bhatla - Cancer of the ce` | cancer_mama | **cancer_cuello_uterino** |
| `Terapéutica+del+cáncer+de+cuello+uterino,+una+revisión+de+la` | cancer_mama | **cancer_cuello_uterino** |
| `cervical-es-patient.pdf` | cancer_mama | **cancer_cuello_uterino** |
| `cervix16nov-full.pdf` | cancer_mama | **cancer_cuello_uterino** |
| `curroncol-29-00755.pdf` | cancer_mama | **cancer_cuello_uterino** |
| `fonc-14-1461296.pdf` | cancer_mama | **cancer_cuello_uterino** |
| `fsurg-12-1588558.pdf` | cancer_mama | **cancer_cuello_uterino** |
| `gom226c.pdf` | cancer_mama | **cancer_cuello_uterino** |
| `ijgm-18-6073.pdf` | cancer_mama | **cancer_cuello_uterino** |
| `main.pdf` | cancer_mama | **cancer_cuello_uterino** |
| `prevencion-cancer-cuello-uterino.pdf` | cancer_mama | **cancer_cuello_uterino** |

## Distribucion por tema detectado

| Tema | Documentos |
|---|---:|
| cancer_colorrectal | 25 |
| apendicitis | 23 |
| artroplastia | 22 |
| cancer_cuello_uterino | 18 |
| colecistitis | 17 |
| sin_clasificar | 1 |

## Cobertura por procedimiento del paciente

Los 40 pacientes del dataset se reparten en cinco procedimientos. Esta tabla dice
si el corpus entregado contiene material del procedimiento de cada uno.

| Procedimiento | Tema requerido | Documentos | Estado |
|---|---|---:|---|
| Apendicectomia | apendicitis | 23 | cubierto |
| Colecistectomia | colecistitis | 17 | cubierto |
| Colectomia | cancer_colorrectal | 25 | cubierto |
| Mastectomia | cancer_mama | 0 | **SIN COBERTURA** |
| Reemplazo de cadera/rodilla | artroplastia | 22 | cubierto |
