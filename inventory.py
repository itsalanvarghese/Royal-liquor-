import csv

def validate_inventory():
    """
    Utility function to validate the CSV inventory file
    """
    try:
        with open('Inventory_Royal_Liquor.csv', 'r', encoding='utf-8-sig') as file:
            reader = csv.DictReader(file)
            required_fields = ['Barcode', 'Name', 'Price']
            
            # Validate headers
            if not all(field in reader.fieldnames for field in required_fields):
                return False, "Missing required fields in CSV"
            
            # Validate data
            for row in reader:
                if not all(row[field].strip() for field in required_fields):
                    return False, f"Missing data in row: {row}"
                
                # Validate barcode format
                if not row['Barcode'].strip().replace('-', '').isdigit():
                    return False, f"Invalid barcode format: {row['Barcode']}"
                
                # Validate price format
                if not row['Price'].strip().startswith('$'):
                    return False, f"Invalid price format: {row['Price']}"
                    
        return True, "Inventory file is valid"
        
    except FileNotFoundError:
        return False, "Inventory file not found"
    except Exception as e:
        return False, f"Error validating inventory: {str(e)}"

if __name__ == "__main__":
    success, message = validate_inventory()
    print(message)
