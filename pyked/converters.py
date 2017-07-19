"""Module with converters from other formats.
"""

# Standard libraries
import os
from argparse import ArgumentParser
from warnings import warn
import numpy

from requests.exceptions import HTTPError, ConnectionError
import habanero

try:
    from lxml import etree
except ImportError:
    try:
        import xml.etree.cElementTree as etree
    except ImportError:
        try:
            import xml.etree.ElementTree as etree
        except ImportError:
            print("Failed to import ElementTree from any known place")
            raise

# Local imports
from .chemked import ChemKED, DataPoint
from .utils import units
from ._version import __version__


# Exceptions
class ParseError(Exception):
    """Base class for errors."""
    pass

class KeywordError(ParseError):
    """Raised for errors in keyword parsing."""

    def __init__(self, *keywords):
        self.keywords = keywords

    def __str__(self):
        return repr('Error: {}.'.format(self.keywords))

class MissingElementError(KeywordError):
    """Raised for missing required elements."""

    def __str__(self):
        return repr('Error: Required element {} is missing.'.format(
            self.keywords))

class MissingAttributeError(KeywordError):
    """Raised for missing required attribute."""

    def __str__(self):
        return repr('Error: Required attribute {} of {} is missing.'.format(
            self.keywords))

class UndefinedKeywordError(KeywordError):
    """Raised for undefined keywords."""

    def __str__(self):
        return repr('Error: Keyword not defined: {}'.format(self.keywords))


def get_file_metadata(root):
    """Read and parse ReSpecTh XML file metadata (file author, version, etc.)

    Parameters
    ----------
    root : ``etree.Element``
        root of ReSpecTh XML file

    Returns
    -------
    properties : dict
        Dictionary with file metadata

    """
    properties = {}

    properties['file-author'] = {'name': ''}
    try:
        properties['file-author']['name'] = root.find('fileAuthor').text
    except AttributeError:
        print('Warning: no fileAuthor given')

    # Default version is 0
    version = '0'
    elem = root.find('fileVersion')
    if elem is None:
        print('Warning: no fileVersion given')
    else:
        try:
            version = elem.find('major').text + '.' + elem.find('minor').text
        except AttributeError:
            print('Warning: missing fileVersion major/minor')
    properties['file-version'] = int(float(version))

    # Default ChemKED version
    properties['chemked-version'] = __version__

    return properties


def get_reference(root):
    """Read reference info from root of ReSpecTh XML file.

    Parameters
    ----------
    root : ``etree.Element``
        root of ReSpecTh XML file

    Returns
    -------
    reference : dict
        Dictionary with reference information

    """
    reference = {}
    elem = root.find('bibliographyLink')

    # Try to get reference info via DOI
    try:
        reference['doi'] = elem.attrib['doi']
    except KeyError:
        print('Warning: missing doi attribute in bibliographyLink')

    if reference.get('doi') is not None:
        ref = None
        try:
            ref = habanero.Crossref().works(ids=reference['doi'])['message']
        except (HTTPError, habanero.RequestError):
            self._error(field, 'DOI not found')
            return
        # TODO: remove UnboundLocalError after habanero fixed
        except (ConnectionError, UnboundLocalError):
            warn('network not available, DOI not found.')

        if ref is not None:
            ## Now get elements of the reference data
            # Assume that the reference returned by the DOI lookup always has a container-title
            reference['journal'] = ref.get('container-title')[0]
            ref_year = ref.get('published-print') or ref.get('published-online')
            reference['year'] = int(ref_year['date-parts'][0][0])
            reference['volume'] = int(ref.get('volume'))
            reference['pages'] = ref.get('page')
            reference['authors'] = []
            for author in ref['author']:
                auth = {}
                auth['name'] = ' '.join([author['given'], author['family']])
                # Add ORCID if available
                orcid = author.get('ORCID')
                if orcid:
                    auth['ORCID'] = orcid
                reference['authors'].append(auth)

    else:
        print('Setting "citation" key as a fallback; please update')
        try:
            reference['citation'] = elem.attrib['preferredKey']
        except KeyError:
            print('Warning: missing preferredKey attribute in bibliographyLink')

    return reference


def get_experiment_kind(root):
    """Read common properties from root of ReSpecTh XML file.

    Parameters
    ----------
    root : ``etree.Element``
        root of ReSpecTh XML file

    Returns
    -------
    properties : dict
        Dictionary with experiment type and apparatus information.

    """
    properties = {}
    if root.find('experimentType').text == 'Ignition delay measurement':
        properties['experiment-type'] = 'ignition delay'
    else:
        #TODO: support additional experimentTypes
        raise KeywordError('experimentType not ignition delay measurement')

    properties['apparatus'] = {'kind': '', 'institution': '', 'facility': ''}
    try:
        kind = root.find('apparatus/kind').text
        if kind in ['shock tube', 'rapid compression machine']:
            properties['apparatus']['kind'] = kind
        else:
            raise NotImplementedError(kind + ' experiment not (yet) supported')
    except:
        raise MissingElementError('apparatus/kind')

    return properties


def get_common_properties(root):
    """Read common properties from root of ReSpecTh XML file.

    Parameters
    ----------
    root : ``etree.Element``
        root of ReSpecTh XML file

    Returns
    -------
    properties : dict
        Dictionary with common properties added

    """
    properties = {}

    for elem in root.iterfind('commonProperties/property'):
        name = elem.attrib['name']

        if name == 'initial composition':
            properties['composition'] = {'species': []}
            composition_type = None

            for child in elem.iter('component'):
                spec = {}
                spec['species-name'] = child.find('speciesLink').attrib['preferredKey']

                # use InChI for unique species identifier (if present)
                try:
                    spec['InChI'] = child.find('speciesLink').attrib['InChI']
                except KeyError:
                    # TODO: add InChI validator/search
                    print('Warning: missing InChI for species ' + spec['species-name'])
                    pass

                # amount of that species
                spec['amount'] = [float(child.find('amount').text)]

                properties['composition']['species'].append(spec)

                # check consistency of composition type
                if not composition_type:
                    composition_type = child.find('amount').attrib['units']
                elif composition_type != child.find('amount').attrib['units']:
                    raise KeywordError('inconsistent initial composition units')
            assert composition_type in ['mole fraction', 'mass fraction']
            properties['composition']['kind'] = composition_type

        elif name == 'temperature':
            # Common initial temperature
            properties['temperature'] = [' '.join([elem.find('value').text, elem.attrib['units']])]

        elif name == 'pressure':
            # Common initial pressure
            units = elem.attrib['units']
            if units == 'Torr':
                units = 'torr'
            properties['pressure'] = [' '.join([elem.find('value').text, units])]

        elif name == 'pressure rise':
            # Constant pressure rise, given in % of initial pressure
            # per unit of time
            if root.find('apparatus/kind').text == 'rapid compression machine':
                raise KeywordError('Pressure rise cannot be defined for RCM.')
            properties['pressure-rise'] = [' '.join([elem.find('value').text, elem.attrib['units']])]

        elif name == 'compression time':
            # RCM compression time, given in time units
            if root.find('apparatus/kind').text == 'shock tube':
                raise KeywordError('Compression time cannot be defined for shock tube.')
            properties['compression-time'] = [' '.join([elem.find('value').text, elem.attrib['units']])]

    return properties


def get_ignition_type(root):
    """Gets ignition type and target.

    Parameters
    ----------
    root : ``etree.Element``
        root of ReSpecTh XML file

    Returns
    -------
    ignition : dict
        Dictionary with ignition type/target information

    """
    ignition = {}
    elem = root.find('ignitionType')

    if elem is None:
        raise MissingElementError('ignitionType')

    try:
        ign_target = elem.attrib['target'].rstrip(';').upper()
    except KeyError:
        raise MissingAttributeError('ignitionType target')
    try:
        ign_type = elem.attrib['type']
    except KeyError:
        raise MissingAttributeError('ignitionType type')

    # ReSpecTh allows multiple ignition targets
    if len(ign_target.split(';')) > 1:
        raise NotImplementedError('Multiple ignition targets not implemented.')

    # Acceptable ignition targets include pressure, temperature, and species
    # concentrations
    if ign_target == 'OHEX':
        ign_target = 'OH*'
    elif ign_target == 'CHEX':
        ign_target = 'CH*'

    if ign_target not in ['P', 'T', 'OH', 'OH*', 'CH*', 'CH']:
        raise UndefinedKeywordError(ign_target)

    if ign_type not in ['max', 'd/dt max',
                        'baseline max intercept from d/dt',
                        'baseline min intercept from d/dt',
                        'concentration', 'relative concentration'
                        ]:
        raise UndefinedKeywordError(ign_type)

    if ign_type in ['baseline max intercept from d/dt',
                    'baseline min intercept from d/dt'
                    ]:
        raise NotImplementedError(ign_type + ' not supported')

    if ign_target == 'P':
        ign_target = 'pressure'
    elif ign_target == 'T':
        ign_target = 'temperature'

    ignition['type'] = ign_type
    ignition['target'] = ign_target

    if ign_type in ['concentration', 'relative concentration']:
        try:
            amt = elem.attrib['amount']
        except KeyError:
            raise MissingAttributeError('ignitionType amount')
        try:
            amt_units = elem.attrib['units']
        except KeyError:
            raise MissingAttributeError('ignitionType units')

        raise NotImplementedError('concentration ignition delay type '
                                  'not supported'
                                  )

    return ignition


def get_datapoints(root):
    """Parse datapoints with ignition delay from file.

    Parameters
    ----------
    root : ``etree.Element``
        root of ReSpecTh XML file

    Returns
    -------
    properties : dict
        Dictionary with ignition delay data

    """
    datapoints = []
    # Shock tube experiment will have one data group, while RCM may have one
    # or two (one for ignition delay, one for volume-history)
    dataGroups = root.findall('dataGroup')

    # all situations will have main experimental data in first dataGroup
    dataGroup = dataGroups[0]
    property_id = {}
    unit_id = {}
    # get properties of dataGroup
    for prop in dataGroup.findall('property'):
        unit_id[prop.attrib['id']] = prop.attrib['units']

        property_id[prop.attrib['id']] = prop.attrib['name']
        if prop.attrib['name'] == 'ignition delay':
            property_id[prop.attrib['id']] = 'ignition-delay'

    # now get data points
    for dp in dataGroup.findall('dataPoint'):
        datapoint = {}
        for val in dp:
            units = unit_id[val.tag]
            if units == 'Torr':
                units = 'torr'
            datapoint[property_id[val.tag]] = [val.text + ' ' + units]
        datapoints.append(datapoint)

    # RCM files may have a second dataGroup with volume-time history
    if len(dataGroups) == 2:
        assert root.find('apparatus/kind').text == 'rapid compression machine'
        assert len(datapoints) == 1

        dataGroup = dataGroups[1]
        for prop in dataGroup.findall('property'):
            if prop.attrib['name'] == 'time':
                time_dict = {'units': prop.attrib['units'], 'column': 0}
                time_tag = prop.attrib['id']
            elif prop.attrib['name'] == 'volume':
                volume_dict = {'units': prop.attrib['units'], 'column': 1}
                volume_tag = prop.attrib['id']

        volume_history = {'time': time_dict, 'volume': volume_dict, 'values': []}

        # collect volume-time history
        for dp in dataGroup.findall('dataPoint'):
            time = None
            volume = None
            for val in dp:
                if val.tag == time_tag:
                    time = float(val.text)
                elif val.tag == volume_tag:
                    volume = float(val.text)
            volume_history['values'].append([time, volume])

        datapoints[0]['volume-history'] = volume_history

    elif len(dataGroups) > 2:
        raise NotImplementedError('More than two DataGroups not supported.')

    return datapoints


def read_experiment(filename):
    """Reads experiment data from ReSpecTh XML file.

    Parameters
    ----------
    filename : str
        XML filename in ReSpecTh format with experimental data

    Returns
    -------
    properties : dict
        Dictionary with group of experimental properties

    """

    try:
        tree = etree.parse(filename)
    except OSError:
        raise OSError('Unable to open file ' + filename)
    root = tree.getroot()

    properties = {}

    # get file metadata
    properties.update(get_file_metadata(root))

    # get reference info
    properties['reference'] = get_reference(root)
    # Save name of original data filename
    properties['reference']['detail'] = 'Converted from ' + os.path.basename(filename)

    # Ensure ignition delay, and get which kind of experiment
    properties.update(get_experiment_kind(root))

    # Get properties shared across the file
    properties['common-properties'] = get_common_properties(root)

    # Determine definition of ignition delay
    properties['common-properties']['ignition-type'] = get_ignition_type(root)

    # Now parse ignition delay datapoints
    properties['datapoints'] = get_datapoints(root)

    # Get compression time for RCM, if volume history given
    # if 'volume' in properties and 'compression-time' not in properties:
    #     min_volume_idx = numpy.argmin(properties['volume'])
    #     min_volume_time = properties['time'][min_volume_idx]
    #     properties['compression-time'] = min_volume_time

    # Ensure combinations of volume, time, pressure-rise are correct.
    if ('volume' in properties['common-properties'] and
        'time' not in properties['common-properties']
        ):
        raise KeywordError('Time values needed for volume history')
    elif (any(['volume' in dp for dp in properties['datapoints']]) and
          not any(['time' in dp for dp in properties['datapoints']])
          ):
        raise KeywordError('Time values needed for volume history')

    if (('volume' in properties['common-properties'] and
         'pressure-rise' in properties['common-properties']
         ) or ('volume' in properties['common-properties'] and
               any([dp for dp in properties['datapoints'] if dp.get('pressure-rise')])
               ) or ('pressure-rise' in properties['common-properties'] and
                     any([dp for dp in properties['datapoints'] if dp.get('volume')])
                     )
        ):
        raise KeywordError('Both volume history and pressure rise '
                           'cannot be specified'
                           )

    return properties


def convert_ReSpecTh_to_ChemKED(filename_xml, output='', file_author='',
                                file_author_orcid=''
                                ):
    """Convert ReSpecTh XML file to ChemKED YAML file.

    Parameters
    ----------
    filename_xml : str
        Name of ReSpecTh XML file to be converted.
    output : str
        Optional; output path for converted file.
    file_author : str
        Optional; name to override original file author
    file_author_orcid : str
        Optional; ORCID of file author

    Returns
    -------
    filename_yaml : str
        Name of newly created ChemKED YAML file.

    """
    assert os.path.isfile(filename_xml), filename_xml + ' file missing'

    # get all information from XML file
    properties = read_experiment(filename_xml)

    # apply any overrides
    if file_author:
        properties['file-author']['name'] = file_author
    if file_author_orcid:
        properties['file-author']['ORCID'] = file_author_orcid

    # Now go through datapoints and apply common properties
    for idx in range(len(properties['datapoints'])):
        for prop in properties['common-properties']:
            properties['datapoints'][idx][prop] = properties['common-properties'][prop]

    filename_yaml = os.path.splitext(os.path.basename(filename_xml))[0] + '.yaml'

    # add path
    filename_yaml = os.path.join(output, filename_yaml)

    with open(filename_yaml, 'w') as outfile:
        outfile.write(yaml.dump(properties, default_flow_style=False))
    print('Converted to ' + filename_yaml)

    # now validate
    ChemKED(yaml_file=filename_yaml)

    return filename_yaml


if __name__ == '__main__':
    parser = ArgumentParser(description='Convert ReSpecTh XML file to ChemKED '
                                        'YAML file.'
                            )
    parser.add_argument('-i', '--input',
                        type=str,
                        required=True,
                        help='Input XML filename'
                        )
    parser.add_argument('-o', '--output',
                        type=str,
                        required=False,
                        default='',
                        help='Output directory for file'
                        )
    parser.add_argument('-fa', '--file-author',
                        dest='file_author',
                        type=str,
                        required=False,
                        default='',
                        help='File author name to override original'
                        )
    parser.add_argument('-fo', '--file-author-orcid',
                        dest='file_author_orcid',
                        type=str,
                        required=False,
                        default='',
                        help='File author ORCID'
                        )

    args = parser.parse_args()
    convert_ReSpecTh_to_ChemKED(args.input, args.output,
                                args.file_author, args.file_author_orcid
                                )